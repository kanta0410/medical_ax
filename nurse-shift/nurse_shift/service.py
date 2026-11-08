"""欠員の状態機械。充足の確定だけが勤務表を書き換え、同じ欠員は一度しか埋まらない。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from nurse_shift.db import Tx, connect, ensure_schema
from nurse_shift.domain import (
    SHIFT_CLOCK,
    SHIFT_LABEL,
    Problem,
    applicant_block,
    coverage_errors,
    demo_problem,
    measure,
    placement_rest_issue,
    rest_summary,
    status_note,
)
from nurse_shift.solver import solve

WEEK = "月火水木金土日"


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


class Conflict(ServiceError):
    def __init__(self, message: str):
        super().__init__(message, 409)


class Service:
    def __init__(self, db_path: str, problem: Problem | None = None):
        self.db_path = db_path
        self.problem = problem or demo_problem()

    def init(self) -> None:
        conn = connect(self.db_path)
        try:
            ensure_schema(conn)
            row = conn.execute("SELECT value FROM meta WHERE key = 'problem'").fetchone()
            if row is not None:
                self.problem = Problem.from_json(row["value"])
                return
        finally:
            conn.close()
        self.generate()

    def reset(self) -> None:
        conn = connect(self.db_path)
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            for table in (
                "application",
                "posting",
                "offer",
                "vacancy",
                "assignment",
                "baseline",
                "preference",
                "staff",
                "meta",
            ):
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            ensure_schema(conn)
        finally:
            conn.close()
        self.generate()

    def generate(self) -> None:
        result = solve(self.problem)
        if result.assignments is None:
            raise ServiceError(status_note(result.code))
        errors = coverage_errors(self.problem, result.assignments)
        if errors:
            raise ServiceError("初期勤務表がデモの絶対条件を満たしません。" + errors[0])
        conn = connect(self.db_path)
        try:
            with Tx(conn):
                self._write_roster(conn, result.assignments, result.code, result.solve_ms)
        finally:
            conn.close()

    def open_vacancy(self, staff_id: str, day: int) -> int:
        conn = connect(self.db_path)
        try:
            people = self.problem.staff_by_id()
            if staff_id not in people:
                raise ServiceError("スタッフが見つかりません", 404)
            if day < 0 or day >= self.problem.days:
                raise ServiceError("対象日がデモの期間外です")
            current = self._assignments(conn)
            shift = current.get((staff_id, day), "OFF")
            if shift == "OFF":
                raise ServiceError("休みのセルは欠勤にできません")
            if conn.execute("SELECT id FROM vacancy WHERE status = 'open'").fetchone():
                raise Conflict("未完了の欠員が残っています。先にその欠員を終えてください")
            baseline = self._baseline(conn)
            external, external_leaders = self._external(conn)
            force_off, force_on = self._locks(conn)
            force_off = force_off + [(staff_id, day)]
            force_on = [item for item in force_on if (item[0], item[1]) != (staff_id, day)]
            repair = solve(
                self.problem,
                baseline=baseline,
                force_off=force_off,
                force_on=force_on,
                external=external,
                external_leaders=external_leaders,
            )
            rows: list[tuple] = []
            rows.append(
                (
                    staff_id,
                    0,
                    None,
                    None,
                    "BLOCKED",
                    "欠勤した本人には再打診しません。",
                    "declined",
                )
            )
            for person in self.problem.staff:
                if person.id == staff_id:
                    continue
                if shift in ("E", "N") and not person.night_ok:
                    rows.append(
                        (
                            person.id,
                            0,
                            None,
                            None,
                            "INFEASIBLE",
                            "この枠は夜勤で、夜勤可の資格がありません。",
                            "declined",
                        )
                    )
                    continue
                forced = solve(
                    self.problem,
                    baseline=baseline,
                    force_off=force_off,
                    force_on=force_on + [(person.id, day, shift)],
                    external=external,
                    external_leaders=external_leaders,
                )
                if forced.code in ("OPTIMAL", "FEASIBLE") and forced.assignments is not None:
                    check = coverage_errors(
                        self.problem,
                        forced.assignments,
                        external,
                        external_leaders,
                    )
                    if check:
                        rows.append(
                            (
                                person.id,
                                0,
                                forced.change_count,
                                forced.preference_violations,
                                forced.code,
                                "再計算した結果、絶対条件を満たしません。" + check[0],
                                "declined",
                            )
                        )
                        continue
                    reason = (
                        f"{person.name}を{SHIFT_LABEL[shift]}に入れる案です。"
                        f"変更セル数は{forced.change_count}、希望違反数は{forced.preference_violations}です。"
                        f"{rest_summary(forced.assignments, person.id, self.problem.rest_minutes)}"
                        f"{status_note(forced.code)}"
                    )
                    rows.append(
                        (
                            person.id,
                            1,
                            forced.change_count,
                            forced.preference_violations,
                            forced.code,
                            reason,
                            "proposed",
                        )
                    )
                else:
                    rest_issue = placement_rest_issue(
                        current, person.id, day, shift, self.problem.rest_minutes
                    )
                    if rest_issue:
                        reason = f"不採用。{rest_issue}{status_note(forced.code)}"
                    else:
                        reason = status_note(forced.code)
                    rows.append(
                        (
                            person.id,
                            0,
                            None,
                            None,
                            forced.code,
                            reason,
                            "declined",
                        )
                    )
            with Tx(conn):
                again = self._assignments(conn)
                if again.get((staff_id, day)) != shift:
                    raise Conflict("勤務表が変わったため、欠員を作り直してください")
                if conn.execute("SELECT id FROM vacancy WHERE status = 'open'").fetchone():
                    raise Conflict("未完了の欠員が残っています。先にその欠員を終えてください")
                conn.execute(
                    "UPDATE assignment SET shift = 'OFF' WHERE staff_id = ? AND day = ?",
                    (staff_id, day),
                )
                cur = conn.execute(
                    """
                    INSERT INTO vacancy (
                      staff_id, day, shift, status, solver_code, created_at
                    ) VALUES (?, ?, ?, 'open', ?, ?)
                    """,
                    (staff_id, day, shift, repair.code, _now()),
                )
                vacancy_id = int(cur.lastrowid)
                conn.executemany(
                    """
                    INSERT INTO offer (
                      vacancy_id, staff_id, eligible, change_count,
                      preference_violations, solver_code, reason, response
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [(vacancy_id, *row) for row in rows],
                )
            return vacancy_id
        finally:
            conn.close()

    def ask(self, vacancy_id: int, staff_id: str) -> None:
        conn = connect(self.db_path)
        try:
            with Tx(conn):
                self._require_open(conn, vacancy_id)
                cur = conn.execute(
                    """
                    UPDATE offer
                    SET response = 'asked'
                    WHERE vacancy_id = ? AND staff_id = ? AND eligible = 1 AND response = 'proposed'
                    """,
                    (vacancy_id, staff_id),
                )
                if cur.rowcount != 1:
                    raise ServiceError("打診できる候補ではありません")
        finally:
            conn.close()

    def respond(self, vacancy_id: int, staff_id: str, decision: str) -> None:
        if decision == "decline":
            self._decline(vacancy_id, staff_id)
            return
        if decision != "accept":
            raise ServiceError("応答は入れるか無理だけです")
        self._accept(vacancy_id, staff_id)

    def confirm_admin(self, vacancy_id: int) -> None:
        conn = connect(self.db_path)
        try:
            with Tx(conn):
                vacancy = self._require_open(conn, vacancy_id)
                if not self._can_admin(conn, vacancy_id):
                    raise ServiceError("内部候補の応答が揃うまで、募集シミュレーションは開けません")
                exists = conn.execute(
                    "SELECT id FROM posting WHERE vacancy_id = ?",
                    (vacancy_id,),
                ).fetchone()
                if exists:
                    return
                label = SHIFT_LABEL[vacancy["shift"]]
                body = (
                    f"欠員#{vacancy_id}の{label}1枠を、画面内だけで募集するシミュレーションです。"
                    "外部の求人媒体へは送っていません。"
                )
                conn.execute(
                    """
                    INSERT INTO posting (vacancy_id, status, admin_confirmed, body)
                    VALUES (?, 'open', 1, ?)
                    """,
                    (vacancy_id, body),
                )
        finally:
            conn.close()

    def mock_apply(self, vacancy_id: int, name: str, night_ok: bool, leader: bool) -> dict:
        conn = connect(self.db_path)
        try:
            with Tx(conn):
                vacancy = self._require_open(conn, vacancy_id)
                posting = conn.execute(
                    "SELECT * FROM posting WHERE vacancy_id = ?",
                    (vacancy_id,),
                ).fetchone()
                if posting is None or not posting["admin_confirmed"] or posting["status"] != "open":
                    raise ServiceError("管理者の確認が済んだ募集だけに、架空の応募を載せられます")
                day = int(vacancy["day"])
                shift = vacancy["shift"]
                leaders_on_day = self._leaders_on_day(conn, day)
                block = applicant_block(shift, night_ok, leader, leaders_on_day)
                if block:
                    conn.execute(
                        """
                        INSERT INTO application (
                          posting_id, applicant_name, night_ok, leader, accepted, reason
                        ) VALUES (?, ?, ?, ?, 0, ?)
                        """,
                        (posting["id"], name, int(night_ok), int(leader), block),
                    )
                    return {"accepted": False, "reason": block}
                have = self._have(conn, day, shift)
                need = self.problem.demand[shift]
                if have >= need:
                    raise Conflict("この枠は既に人数が埋まっています")
                if have != need - 1:
                    raise ServiceError(f"この枠は{have}人で、デモ設定の{need}人に対して欠員1人ではありません")
                cur = conn.execute(
                    """
                    UPDATE vacancy
                    SET status = 'filled', fill_route = 'external',
                        filled_external_name = ?, filled_external_leader = ?,
                        filled_external_night_ok = ?, commit_code = 'CHECKED'
                    WHERE id = ? AND status = 'open'
                    """,
                    (name, int(leader), int(night_ok), vacancy_id),
                )
                if cur.rowcount != 1:
                    raise Conflict("この欠員は既に充足しています")
                conn.execute(
                    """
                    INSERT INTO application (
                      posting_id, applicant_name, night_ok, leader, accepted, reason
                    ) VALUES (?, ?, ?, ?, 1, ?)
                    """,
                    (
                        posting["id"],
                        name,
                        int(night_ok),
                        int(leader),
                        "資格と人数を確認し、この欠員に入れました。",
                    ),
                )
                conn.execute(
                    "UPDATE posting SET status = 'closed' WHERE id = ?",
                    (posting["id"],),
                )
                return {"accepted": True, "reason": "資格と人数を確認し、この欠員に入れました。"}
        finally:
            conn.close()

    def snapshot(self) -> dict:
        conn = connect(self.db_path)
        try:
            meta = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM meta")}
            assignments = self._assignments(conn)
            baseline = self._baseline(conn)
            measured = measure(self.problem, baseline, assignments)
            unfilled = conn.execute(
                "SELECT COUNT(*) AS n FROM vacancy WHERE status != 'filled'"
            ).fetchone()["n"]
            counts = self._count_map(conn, assignments)
            start = date.fromisoformat(self.problem.start_date)
            dates = []
            for day in range(self.problem.days):
                current = start + timedelta(days=day)
                dates.append(
                    {
                        "day": day,
                        "label": f"{current.month}/{current.day}({WEEK[current.weekday()]})",
                    }
                )
            people = self.problem.staff_by_id()
            vacancies = []
            vacancy_rows = conn.execute("SELECT * FROM vacancy ORDER BY id").fetchall()
            open_by_cell = {
                (row["staff_id"], row["day"]): row
                for row in vacancy_rows
                if row["status"] == "open"
            }
            filled_by_cell = {
                (row["staff_id"], row["day"]): row
                for row in vacancy_rows
                if row["status"] == "filled"
            }
            for row in vacancy_rows:
                offers = conn.execute(
                    """
                    SELECT * FROM offer
                    WHERE vacancy_id = ?
                    ORDER BY eligible DESC, change_count, preference_violations, staff_id
                    """,
                    (row["id"],),
                ).fetchall()
                posting = conn.execute(
                    "SELECT * FROM posting WHERE vacancy_id = ?",
                    (row["id"],),
                ).fetchone()
                applications = []
                if posting is not None:
                    applications = [
                        dict(item)
                        for item in conn.execute(
                            "SELECT * FROM application WHERE posting_id = ? ORDER BY id",
                            (posting["id"],),
                        )
                    ]
                day = int(row["day"])
                shift = row["shift"]
                count = counts[(day, shift)]
                eligible = []
                ineligible = []
                for offer in offers:
                    item = {
                        "staff_id": offer["staff_id"],
                        "name": people[offer["staff_id"]].name,
                        "eligible": bool(offer["eligible"]),
                        "change_count": offer["change_count"],
                        "preference_violations": offer["preference_violations"],
                        "solver_code": offer["solver_code"],
                        "reason": offer["reason"],
                        "response": offer["response"],
                    }
                    if offer["eligible"]:
                        eligible.append(item)
                    else:
                        ineligible.append(item)
                vacancies.append(
                    {
                        "id": row["id"],
                        "staff_id": row["staff_id"],
                        "staff_name": people[row["staff_id"]].name,
                        "day": day,
                        "date_label": dates[day]["label"],
                        "shift": shift,
                        "shift_label": SHIFT_LABEL[shift],
                        "shift_time": SHIFT_CLOCK[shift],
                        "status": row["status"],
                        "solver_code": row["solver_code"],
                        "solver_note": status_note(row["solver_code"]),
                        "fill_route": row["fill_route"],
                        "filled_staff_id": row["filled_staff_id"],
                        "filled_external_name": row["filled_external_name"],
                        "commit_code": row["commit_code"],
                        "have": count["have"],
                        "need": count["need"],
                        "short": count["short"],
                        "leaders_on_day": self._leaders_on_day(conn, day),
                        "offers": eligible,
                        "ineligible": ineligible,
                        "can_admin": self._can_admin(conn, row["id"]),
                        "posting": None
                        if posting is None
                        else {
                            "id": posting["id"],
                            "status": posting["status"],
                            "body": posting["body"],
                            "applications": applications,
                        },
                    }
                )
            staff_rows = []
            for person in self.problem.staff:
                cells = []
                for day in range(self.problem.days):
                    shift = assignments.get((person.id, day), "OFF")
                    open_vacancy = open_by_cell.get((person.id, day))
                    filled = filled_by_cell.get((person.id, day))
                    preference = (person.id, day) in self.problem.preferences and shift != "OFF"
                    if open_vacancy is not None:
                        short = counts[(day, open_vacancy["shift"])]["short"]
                        text = f"不足{short}人・欠員#{open_vacancy['id']}・{SHIFT_LABEL[open_vacancy['shift']]}"
                        kind = "short"
                        vacancy_id = open_vacancy["id"]
                        workable = False
                    elif filled is not None and filled["fill_route"] == "external":
                        text = f"充足 {filled['filled_external_name']}・欠員#{filled['id']}"
                        kind = "filled"
                        vacancy_id = filled["id"]
                        workable = False
                    elif filled is not None:
                        text = f"欠勤・欠員#{filled['id']}"
                        kind = "absent"
                        vacancy_id = filled["id"]
                        workable = False
                    else:
                        text = SHIFT_LABEL[shift]
                        if preference:
                            text += "・希望外"
                        kind = shift
                        vacancy_id = None
                        workable = shift in ("D", "E", "N")
                    cells.append(
                        {
                            "day": day,
                            "shift": shift,
                            "text": text,
                            "kind": kind,
                            "vacancy_id": vacancy_id,
                            "workable": workable,
                        }
                    )
                staff_rows.append(
                    {
                        "id": person.id,
                        "name": person.name,
                        "leader": person.leader,
                        "night_ok": person.night_ok,
                        "role": _role(person.leader, person.night_ok),
                        "cells": cells,
                    }
                )
            coverage = []
            for day in range(self.problem.days):
                shifts = []
                for code in ("D", "E", "N"):
                    item = counts[(day, code)]
                    shifts.append(
                        {
                            "code": code,
                            "label": SHIFT_LABEL[code],
                            "have": item["have"],
                            "need": item["need"],
                            "short": item["short"],
                        }
                    )
                coverage.append({"day": day, "label": dates[day]["label"], "shifts": shifts})
            any_open = any(row["status"] == "open" for row in vacancy_rows)
            for person in staff_rows:
                for cell in person["cells"]:
                    if any_open:
                        cell["workable"] = False
            return {
                "facility": self.problem.facility,
                "banner": "架空データ・外部送信なし",
                "disclaimer": "勤務条件はデモ用の設定です。法令適合、削減効果、現場の最適性は保証しません。",
                "rest_hours": self.problem.rest_minutes // 60,
                "shift_legend": [
                    {"code": code, "label": SHIFT_LABEL[code], "time": SHIFT_CLOCK[code]}
                    for code in ("D", "E", "N")
                ],
                "metrics": {
                    "changed_cells": measured["changed_cells"],
                    "unfilled": unfilled,
                    "preference_violations": measured["preference_violations"],
                },
                "roster": {
                    "code": meta.get("roster_code", ""),
                    "note": status_note(meta.get("roster_code", "")),
                    "solve_ms": int(meta.get("roster_ms", "0")),
                },
                "dates": dates,
                "coverage": coverage,
                "staff": staff_rows,
                "vacancies": vacancies,
                "accepting_absence": not any_open,
            }
        finally:
            conn.close()

    def _accept(self, vacancy_id: int, staff_id: str) -> None:
        conn = connect(self.db_path)
        try:
            with Tx(conn):
                vacancy = self._require_open(conn, vacancy_id)
                offer = conn.execute(
                    """
                    SELECT * FROM offer
                    WHERE vacancy_id = ? AND staff_id = ? AND eligible = 1
                    """,
                    (vacancy_id, staff_id),
                ).fetchone()
                if offer is None or offer["response"] != "asked":
                    raise ServiceError("打診した候補だけを確定できます")
                baseline = self._baseline(conn)
                external, external_leaders = self._external(conn)
                force_off, force_on = self._locks(conn)
                result = solve(
                    self.problem,
                    baseline=baseline,
                    force_off=force_off,
                    force_on=force_on + [(staff_id, int(vacancy["day"]), vacancy["shift"])],
                    external=external,
                    external_leaders=external_leaders,
                )
                if result.assignments is None:
                    raise ServiceError("再検証では入れられません。" + status_note(result.code))
                errors = coverage_errors(
                    self.problem, result.assignments, external, external_leaders
                )
                if errors:
                    raise ServiceError("再検証で絶対条件を満たしません。" + errors[0])
                key = (staff_id, int(vacancy["day"]))
                if result.assignments.get(key) != vacancy["shift"]:
                    raise ServiceError("再検証の結果、この人はその枠に入っていません")
                self._replace_assignments(conn, result.assignments)
                cur = conn.execute(
                    """
                    UPDATE vacancy
                    SET status = 'filled', fill_route = 'internal', filled_staff_id = ?,
                        commit_code = ?
                    WHERE id = ? AND status = 'open'
                    """,
                    (staff_id, result.code, vacancy_id),
                )
                if cur.rowcount != 1:
                    raise Conflict("この欠員は既に充足しています")
                conn.execute(
                    """
                    UPDATE offer SET response = 'accepted'
                    WHERE vacancy_id = ? AND staff_id = ?
                    """,
                    (vacancy_id, staff_id),
                )
        finally:
            conn.close()

    def _decline(self, vacancy_id: int, staff_id: str) -> None:
        conn = connect(self.db_path)
        try:
            with Tx(conn):
                self._require_open(conn, vacancy_id)
                cur = conn.execute(
                    """
                    UPDATE offer SET response = 'declined'
                    WHERE vacancy_id = ? AND staff_id = ? AND eligible = 1 AND response = 'asked'
                    """,
                    (vacancy_id, staff_id),
                )
                if cur.rowcount != 1:
                    raise ServiceError("応答できる打診がありません")
        finally:
            conn.close()

    def _write_roster(self, conn, assignments: dict[tuple[str, int], str], code: str, solve_ms: int) -> None:
        for person in self.problem.staff:
            conn.execute(
                "INSERT INTO staff (id, name, leader, night_ok) VALUES (?, ?, ?, ?)",
                (person.id, person.name, int(person.leader), int(person.night_ok)),
            )
        for staff_id, day in self.problem.preferences:
            conn.execute(
                "INSERT INTO preference (staff_id, day) VALUES (?, ?)",
                (staff_id, day),
            )
        for (staff_id, day), shift in assignments.items():
            conn.execute(
                "INSERT INTO baseline (staff_id, day, shift) VALUES (?, ?, ?)",
                (staff_id, day, shift),
            )
            conn.execute(
                "INSERT INTO assignment (staff_id, day, shift) VALUES (?, ?, ?)",
                (staff_id, day, shift),
            )
        conn.execute("INSERT INTO meta (key, value) VALUES ('problem', ?)", (self.problem.to_json(),))
        conn.execute("INSERT INTO meta (key, value) VALUES ('roster_code', ?)", (code,))
        conn.execute("INSERT INTO meta (key, value) VALUES ('roster_ms', ?)", (str(solve_ms),))

    def _replace_assignments(self, conn, assignments: dict[tuple[str, int], str]) -> None:
        for (staff_id, day), shift in assignments.items():
            conn.execute(
                """
                INSERT INTO assignment (staff_id, day, shift) VALUES (?, ?, ?)
                ON CONFLICT(staff_id, day) DO UPDATE SET shift = excluded.shift
                """,
                (staff_id, day, shift),
            )

    def _assignments(self, conn) -> dict[tuple[str, int], str]:
        return {
            (row["staff_id"], row["day"]): row["shift"]
            for row in conn.execute("SELECT staff_id, day, shift FROM assignment")
        }

    def _baseline(self, conn) -> dict[tuple[str, int], str]:
        return {
            (row["staff_id"], row["day"]): row["shift"]
            for row in conn.execute("SELECT staff_id, day, shift FROM baseline")
        }

    def _external(self, conn) -> tuple[dict[tuple[int, str], int], dict[int, int]]:
        external: dict[tuple[int, str], int] = {}
        leaders: dict[int, int] = {}
        rows = conn.execute(
            """
            SELECT day, shift, filled_external_leader
            FROM vacancy
            WHERE status = 'filled' AND fill_route = 'external'
            """
        )
        for row in rows:
            key = (row["day"], row["shift"])
            external[key] = external.get(key, 0) + 1
            if row["shift"] == "D" and row["filled_external_leader"]:
                leaders[row["day"]] = leaders.get(row["day"], 0) + 1
        return external, leaders

    def _locks(self, conn) -> tuple[list[tuple[str, int]], list[tuple[str, int, str]]]:
        force_off: list[tuple[str, int]] = []
        force_on: list[tuple[str, int, str]] = []
        rows = conn.execute(
            "SELECT staff_id, day, shift, status, fill_route, filled_staff_id FROM vacancy"
        )
        for row in rows:
            if row["status"] not in ("open", "filled"):
                continue
            force_off.append((row["staff_id"], row["day"]))
            if row["status"] == "filled" and row["fill_route"] == "internal" and row["filled_staff_id"]:
                force_on.append((row["filled_staff_id"], row["day"], row["shift"]))
        # 代わりに入った人がその日にさらに欠勤した場合、欠勤（force_off）を優先する。
        # 両方を残すと矛盾した制約になり、以降の再計算がすべて INFEASIBLE になる。
        off = set(force_off)
        force_on = [item for item in force_on if (item[0], item[1]) not in off]
        return force_off, force_on

    def _require_open(self, conn, vacancy_id: int):
        row = conn.execute("SELECT * FROM vacancy WHERE id = ?", (vacancy_id,)).fetchone()
        if row is None:
            raise ServiceError("欠員が見つかりません", 404)
        if row["status"] != "open":
            raise Conflict("この欠員は既に充足しています")
        return row

    def _can_admin(self, conn, vacancy_id: int) -> bool:
        vacancy = conn.execute("SELECT status FROM vacancy WHERE id = ?", (vacancy_id,)).fetchone()
        if vacancy is None or vacancy["status"] != "open":
            return False
        if conn.execute("SELECT id FROM posting WHERE vacancy_id = ?", (vacancy_id,)).fetchone():
            return False
        rows = list(
            conn.execute(
                "SELECT eligible, solver_code, response FROM offer WHERE vacancy_id = ?",
                (vacancy_id,),
            )
        )
        if any(row["solver_code"] == "UNKNOWN" for row in rows):
            return False
        eligible = [row for row in rows if row["eligible"]]
        if any(row["response"] in ("proposed", "asked", "accepted") for row in eligible):
            return False
        return all(row["response"] == "declined" for row in eligible)

    def _leaders_on_day(self, conn, day: int) -> int:
        people = {s.id for s in self.problem.staff if s.leader}
        assignments = self._assignments(conn)
        _external, external_leaders = self._external(conn)
        return sum(1 for staff_id in people if assignments.get((staff_id, day)) == "D") + external_leaders.get(
            day, 0
        )

    def _have(self, conn, day: int, shift: str) -> int:
        assignments = self._assignments(conn)
        external, _leaders = self._external(conn)
        have = sum(1 for s in self.problem.staff if assignments.get((s.id, day)) == shift)
        return have + external.get((day, shift), 0)

    def _count_map(self, conn, assignments: dict[tuple[str, int], str]) -> dict[tuple[int, str], dict[str, int]]:
        external, _leaders = self._external(conn)
        counts = {}
        for day in range(self.problem.days):
            for shift, need in self.problem.demand.items():
                have = sum(1 for s in self.problem.staff if assignments.get((s.id, day)) == shift)
                have += external.get((day, shift), 0)
                counts[(day, shift)] = {"have": have, "need": need, "short": max(need - have, 0)}
        return counts


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _role(leader: bool, night_ok: bool) -> str:
    parts = []
    if leader:
        parts.append("リーダー")
    parts.append("夜勤可" if night_ok else "夜勤不可")
    return "・".join(parts)
