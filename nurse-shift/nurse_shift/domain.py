"""架空病棟の勤務間隔と人数。法令の判定はしない。"""

from __future__ import annotations

from dataclasses import dataclass
import json

SHIFTS = ("D", "E", "N")
SHIFT_LABEL = {"D": "日勤", "E": "準夜", "N": "深夜", "OFF": "休み"}
SHIFT_CLOCK = {
    "D": "8:30–17:00",
    "E": "16:30–翌1:00",
    "N": "0:30–9:00",
}


@dataclass(frozen=True)
class Staff:
    id: str
    name: str
    leader: bool
    night_ok: bool

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "leader": self.leader,
            "night_ok": self.night_ok,
        }

    @staticmethod
    def from_json(data: dict) -> "Staff":
        return Staff(
            id=data["id"],
            name=data["name"],
            leader=bool(data["leader"]),
            night_ok=bool(data["night_ok"]),
        )


@dataclass(frozen=True)
class Problem:
    facility: str
    start_date: str
    staff: tuple[Staff, ...]
    days: int
    demand: dict[str, int]
    preferences: frozenset[tuple[str, int]]
    rest_minutes: int = 11 * 60

    def staff_by_id(self) -> dict[str, Staff]:
        return {s.id: s for s in self.staff}

    def to_json(self) -> str:
        payload = {
            "facility": self.facility,
            "start_date": self.start_date,
            "staff": [s.to_json() for s in self.staff],
            "days": self.days,
            "demand": self.demand,
            "preferences": [[sid, day] for sid, day in sorted(self.preferences)],
            "rest_minutes": self.rest_minutes,
        }
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def from_json(raw: str) -> "Problem":
        data = json.loads(raw)
        return Problem(
            facility=data["facility"],
            start_date=data["start_date"],
            staff=tuple(Staff.from_json(s) for s in data["staff"]),
            days=int(data["days"]),
            demand={k: int(v) for k, v in data["demand"].items()},
            preferences=frozenset((sid, int(day)) for sid, day in data["preferences"]),
            rest_minutes=int(data["rest_minutes"]),
        )


def demo_problem() -> Problem:
    names = [
        "青葉 朝",
        "青葉 碧",
        "青葉 紬",
        "青葉 律",
        "青葉 恵",
        "青葉 楓",
        "青葉 凛",
        "青葉 瞳",
        "青葉 泉",
        "青葉 隼",
        "青葉 葵",
        "青葉 蓮",
    ]
    staff = []
    for i, name in enumerate(names):
        staff.append(
            Staff(
                id=f"n{i + 1:02d}",
                name=name,
                leader=i < 3,
                night_ok=i < 9,
            )
        )
    preferences = frozenset(
        (f"n{i + 1:02d}", (i * 2) % 14) for i in range(len(staff))
    ) | frozenset((f"n{i + 1:02d}", (i * 2 + 5) % 14) for i in range(len(staff)))
    return Problem(
        facility="青葉ケ丘病院 西3病棟（架空の看護病棟）",
        start_date="2026-11-16",
        staff=tuple(staff),
        days=14,
        demand={"D": 3, "E": 2, "N": 2},
        preferences=preferences,
    )


def shift_window(day: int, shift: str) -> tuple[int, int]:
    """分単位の半開区間 [start, end)。day 0 の 0:00 を 0 とする。"""
    base = day * 24 * 60
    if shift == "D":
        return base + 8 * 60 + 30, base + 17 * 60
    if shift == "E":
        return base + 16 * 60 + 30, base + 24 * 60 + 60
    if shift == "N":
        return base + 30, base + 9 * 60
    raise ValueError(shift)


def clock(minutes: int) -> str:
    day, minute = divmod(minutes, 24 * 60)
    hour, mm = divmod(minute, 60)
    return f"{day + 1}日目 {hour:02d}:{mm:02d}"


def pair_issue(day1: int, shift1: str, day2: int, shift2: str, rest_minutes: int) -> str | None:
    a0, a1 = shift_window(day1, shift1)
    b0, b1 = shift_window(day2, shift2)
    if a0 > b0 or (a0 == b0 and a1 > b1):
        day1, shift1, day2, shift2 = day2, shift2, day1, shift1
        a0, a1, b0, b1 = b0, b1, a0, a1
    if a1 > b0:
        return (
            f"重複: {SHIFT_LABEL[shift1]}（{clock(a0)}–{clock(a1)}）と"
            f"{SHIFT_LABEL[shift2]}（{clock(b0)}–{clock(b1)}）"
        )
    gap = b0 - a1
    if gap < rest_minutes:
        hours = gap / 60
        need = rest_minutes / 60
        return (
            f"休息 {hours:.1f} 時間はデモ設定の {need:.0f} 時間未満です"
            f"（{SHIFT_LABEL[shift1]} 終了 {clock(a1)} から {SHIFT_LABEL[shift2]} 開始 {clock(b0)}）"
        )
    return None


def rest_summary(assignments: dict[tuple[str, int], str], staff_id: str, rest_minutes: int) -> str:
    worked = sorted(
        (day, shift)
        for (sid, day), shift in assignments.items()
        if sid == staff_id and shift in SHIFTS
    )
    if len(worked) < 2:
        return "この人の勤務は1件以下で、間隔の比較はありません。"
    worst: tuple[int, str] | None = None
    for i in range(len(worked)):
        for j in range(i + 1, len(worked)):
            issue = pair_issue(worked[i][0], worked[i][1], worked[j][0], worked[j][1], rest_minutes)
            if issue and issue.startswith("重複"):
                return issue if issue.endswith("。") else issue + "。"
            a0, a1 = shift_window(worked[i][0], worked[i][1])
            b0, b1 = shift_window(worked[j][0], worked[j][1])
            if a0 > b0:
                a0, a1, b0, b1 = b0, b1, a0, a1
            gap = b0 - a1
            text = issue or f"最短 {gap / 60:.1f} 時間"
            if worst is None or gap < worst[0]:
                worst = (gap, text)
    assert worst is not None
    if worst[0] < rest_minutes:
        return worst[1] if worst[1].endswith("。") else worst[1] + "。"
    return (
        f"この人の勤務間隔の最短は {worst[0] / 60:.1f} 時間で、"
        f"デモ設定の {rest_minutes / 60:.0f} 時間以上です。"
    )


def placement_rest_issue(
    assignments: dict[tuple[str, int], str],
    staff_id: str,
    day: int,
    shift: str,
    rest_minutes: int,
) -> str | None:
    """この日だけを差し替えて他の勤務を残したとき、休息または重複があればその実時間を返す。"""
    others = [
        (other_day, other_shift)
        for (sid, other_day), other_shift in assignments.items()
        if sid == staff_id and other_day != day and other_shift in SHIFTS
    ]
    worst: tuple[int, str] | None = None
    for other_day, other_shift in others:
        issue = pair_issue(other_day, other_shift, day, shift, rest_minutes)
        if issue is None:
            continue
        left = shift_window(other_day, other_shift)
        right = shift_window(day, shift)
        if left[0] <= right[0]:
            earlier, later = other_shift, shift
            start, end = left[1], right[0]
        else:
            earlier, later = shift, other_shift
            start, end = right[1], left[0]
        need = rest_minutes / 60
        if end < start:
            text = (
                f"{SHIFT_LABEL[earlier]}と{SHIFT_LABEL[later]}が重なります。"
                f"{issue}。"
                f"デモの休息{need:.0f}時間は架空の設定です。"
            )
            return text
        hours = (end - start) / 60
        text = (
            f"{SHIFT_LABEL[earlier]}の終了から{SHIFT_LABEL[later]}の開始まで{hours:.1f}時間です。"
            f"デモ設定は{need:.0f}時間以上ですが、この{need:.0f}時間は架空の設定です。"
        )
        if worst is None or (end - start) < worst[0]:
            worst = (end - start, text)
    if worst is None:
        return None
    return worst[1]


def min_gap_minutes(assignments: dict[tuple[str, int], str], staff_id: str) -> int | None:
    worked = sorted(
        (day, shift)
        for (sid, day), shift in assignments.items()
        if sid == staff_id and shift in SHIFTS
    )
    gaps: list[int] = []
    for i in range(len(worked)):
        for j in range(i + 1, len(worked)):
            d1, s1 = worked[i]
            d2, s2 = worked[j]
            a0, a1 = shift_window(d1, s1)
            b0, b1 = shift_window(d2, s2)
            if a0 > b0:
                a0, a1, b0, b1 = b0, b1, a0, a1
            if a1 > b0:
                return None
            gaps.append(b0 - a1)
    if not gaps:
        return None
    return min(gaps)


def measure(problem: Problem, baseline: dict[tuple[str, int], str], current: dict[tuple[str, int], str]) -> dict[str, int]:
    changed = 0
    for staff in problem.staff:
        for day in range(problem.days):
            key = (staff.id, day)
            if baseline.get(key, "OFF") != current.get(key, "OFF"):
                changed += 1
    pref = 0
    for staff_id, day in problem.preferences:
        if current.get((staff_id, day), "OFF") != "OFF":
            pref += 1
    return {"changed_cells": changed, "preference_violations": pref}


def coverage_errors(
    problem: Problem,
    assignments: dict[tuple[str, int], str],
    external: dict[tuple[int, str], int] | None = None,
    external_leaders: dict[int, int] | None = None,
) -> list[str]:
    external = external or {}
    external_leaders = external_leaders or {}
    errors: list[str] = []
    leaders = {s.id for s in problem.staff if s.leader}
    night = {s.id for s in problem.staff if s.night_ok}
    seen: set[tuple[str, int]] = set()
    for (staff_id, day), shift in assignments.items():
        if day < 0 or day >= problem.days:
            errors.append(f"範囲外の日: {day}")
            continue
        seen.add((staff_id, day))
        if shift not in SHIFTS and shift != "OFF":
            errors.append(f"不明な勤務: {shift}")
        if shift in ("E", "N") and staff_id not in night:
            errors.append(f"{staff_id} は夜勤可ではないのに {SHIFT_LABEL[shift]} です")
    for staff in problem.staff:
        for day in range(problem.days):
            if (staff.id, day) not in assignments:
                errors.append(f"割当がありません: {staff.id} {day}")
    for staff in problem.staff:
        worked = [
            (day, assignments[(staff.id, day)])
            for day in range(problem.days)
            if assignments.get((staff.id, day)) in SHIFTS
        ]
        for i in range(len(worked)):
            for j in range(i + 1, len(worked)):
                issue = pair_issue(worked[i][0], worked[i][1], worked[j][0], worked[j][1], problem.rest_minutes)
                if issue:
                    errors.append(f"{staff.name}: {issue}")
    for day in range(problem.days):
        for shift, need in problem.demand.items():
            have = sum(1 for s in problem.staff if assignments.get((s.id, day)) == shift)
            have += external.get((day, shift), 0)
            if have != need:
                errors.append(f"{day}日目 {SHIFT_LABEL[shift]} は {have} 人で、デモ設定の {need} 人と違います")
        if problem.demand.get("D", 0) > 0:
            leader_have = sum(1 for sid in leaders if assignments.get((sid, day)) == "D")
            leader_have += external_leaders.get(day, 0)
            if leader_have < 1:
                errors.append(f"{day}日目の日勤にリーダーがいません")
    return errors


def status_note(code: str) -> str:
    if code == "OPTIMAL":
        return "求解状態: OPTIMAL。デモモデルに対するCP-SATの状態であり、法令適合や現場の最適性の保証ではありません。"
    if code == "FEASIBLE":
        return "求解状態: FEASIBLE。最適性は未証明です。法令適合の保証ではありません。"
    if code == "INFEASIBLE":
        return "求解状態: INFEASIBLE。このデモモデルでは解なしです。"
    if code == "UNKNOWN":
        return "求解状態: UNKNOWN。時間内に確定せず、解なし（INFEASIBLE）とは別です。"
    return f"求解状態: {code}。デモの求解状態であり、法令適合の保証ではありません。"


def applicant_block(shift: str, night_ok: bool, leader: bool, leaders_on_day: int) -> str | None:
    if shift in ("E", "N") and not night_ok:
        return "この枠は夜勤です。応募者は夜勤可ではないため入れません。"
    if shift == "D" and leaders_on_day <= 0 and not leader:
        return "この日勤はリーダーが無人になります。応募者はリーダー資格がないため入れません。"
    return None
