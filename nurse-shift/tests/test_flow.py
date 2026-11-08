from pathlib import Path
import json
import shutil
import subprocess
import sys
import threading

import pytest
from fastapi.testclient import TestClient

from nurse_shift.domain import Problem, Staff, coverage_errors, measure
from nurse_shift.main import create_app
from nurse_shift.service import Conflict, Service, ServiceError
from tests.record import record


def _problem(staff: tuple[Staff, ...], days: int, demand: dict[str, int]) -> Problem:
    return Problem(
        facility="架空病棟",
        start_date="2026-11-16",
        staff=staff,
        days=days,
        demand=demand,
        preferences=frozenset({("a", 0)}),
    )


def _leaders(nights: tuple[bool, bool, bool]) -> tuple[Staff, ...]:
    return tuple(
        Staff(sid, f"架空{sid}", leader=True, night_ok=night)
        for sid, night in zip(("a", "b", "c"), nights)
    )


def _service(tmp_path: Path, problem: Problem) -> Service:
    service = Service(str(tmp_path / "ward.sqlite"), problem)
    service.init()
    return service


def _working(snapshot: dict, shift: str) -> tuple[str, int]:
    for person in snapshot["staff"]:
        for cell in person["cells"]:
            if cell["shift"] == shift:
                return person["id"], cell["day"]
    raise AssertionError(shift)


def test_internal_accept_fills_and_matches_measured_counts(tmp_path: Path):
    service = _service(tmp_path, _problem(_leaders((True, True, True)), 1, {"D": 1, "E": 0, "N": 0}))
    before = service.snapshot()
    staff_id, day = _working(before, "D")
    vacancy_id = service.open_vacancy(staff_id, day)
    opened = service.snapshot()
    vacancy = opened["vacancies"][0]
    assert vacancy["id"] == vacancy_id
    assert vacancy["short"] == 1
    assert f"不足{vacancy['short']}人" in next(
        cell["text"]
        for person in opened["staff"]
        if person["id"] == staff_id
        for cell in person["cells"]
        if cell["day"] == day
    )
    assert opened["metrics"]["unfilled"] == 1
    baseline, current = _tables(service)
    assert opened["metrics"]["changed_cells"] == measure(service.problem, baseline, current)["changed_cells"]
    assert all(offer["staff_id"] != staff_id for offer in vacancy["offers"])
    chosen = vacancy["offers"][0]
    service.ask(vacancy_id, chosen["staff_id"])
    service.respond(vacancy_id, chosen["staff_id"], "accept")
    filled = service.snapshot()
    assert filled["vacancies"][0]["status"] == "filled"
    assert filled["vacancies"][0]["fill_route"] == "internal"
    assert filled["metrics"]["unfilled"] == 0
    assert filled["coverage"][day]["shifts"][0]["have"] == 1
    assert filled["coverage"][day]["shifts"][0]["short"] == 0
    _baseline, assignments = _tables(service)
    assert coverage_errors(service.problem, assignments) == []
    assert assignments[(staff_id, day)] == "OFF"
    assert assignments[(chosen["staff_id"], day)] == "D"
    with pytest.raises(Conflict) as duplicated:
        service.respond(vacancy_id, chosen["staff_id"], "accept")
    record(
        "internal_fill",
        inputs={
            "staff": [s.id for s in service.problem.staff],
            "demand": service.problem.demand,
            "days": service.problem.days,
            "absent": {"staff_id": staff_id, "day": day, "shift": "D"},
            "accepted_offer": chosen["staff_id"],
        },
        operations=[
            "Service.init CP-SAT roster",
            f"open_vacancy({staff_id}, {day})",
            f"ask({vacancy_id}, {chosen['staff_id']})",
            f"respond({vacancy_id}, {chosen['staff_id']}, accept)",
        ],
        expected={
            "status": "filled",
            "fill_route": "internal",
            "unfilled": 0,
            "shift_have": 1,
            "absent_shift": "OFF",
            "accepted_shift": "D",
            "coverage_errors": [],
        },
        actual={
            "vacancy": filled["vacancies"][0],
            "metrics": filled["metrics"],
            "assignments": {f"{sid}:{d}": shift for (sid, d), shift in assignments.items()},
            "second_accept_status": duplicated.value.status,
            "second_accept_message": duplicated.value.message,
        },
    )


def test_all_decline_then_mock_applicant_fills_same_vacancy(tmp_path: Path):
    service = _service(tmp_path, _problem(_leaders((True, True, True)), 1, {"D": 1, "E": 0, "N": 0}))
    staff_id, day = _working(service.snapshot(), "D")
    vacancy_id = service.open_vacancy(staff_id, day)
    offers = service.snapshot()["vacancies"][0]["offers"]
    assert offers
    for offer in offers:
        service.ask(vacancy_id, offer["staff_id"])
        service.respond(vacancy_id, offer["staff_id"], "decline")
    with pytest.raises(ServiceError):
        service.mock_apply(vacancy_id, "桐谷 透", True, True)
    service.confirm_admin(vacancy_id)
    rejected = service.mock_apply(vacancy_id, "架空 資格不足", False, False)
    assert rejected["accepted"] is False
    assert service.snapshot()["vacancies"][0]["status"] == "open"
    accepted = service.mock_apply(vacancy_id, "桐谷 透", True, True)
    assert accepted["accepted"] is True
    snap = service.snapshot()
    vacancy = snap["vacancies"][0]
    assert vacancy["id"] == vacancy_id
    assert vacancy["status"] == "filled"
    assert vacancy["fill_route"] == "external"
    assert vacancy["filled_external_name"] == "桐谷 透"
    assert vacancy["posting"]["status"] == "closed"
    assert vacancy["posting"]["id"]
    assert snap["metrics"]["unfilled"] == 0
    assert snap["coverage"][day]["shifts"][0]["short"] == 0
    with pytest.raises(Conflict):
        service.mock_apply(vacancy_id, "桐谷 透", True, True)
    record(
        "external_mock_fill",
        inputs={
            "staff": [s.id for s in service.problem.staff],
            "demand": service.problem.demand,
            "absent": {"staff_id": staff_id, "day": day},
            "applicant": {"name": "桐谷 透", "night_ok": True, "leader": True},
        },
        operations=[
            f"open_vacancy({staff_id}, {day})",
            "ask and decline every eligible offer",
            "mock_apply before admin (must fail)",
            "confirm_admin",
            "mock_apply 架空 資格不足 leader=false",
            "mock_apply 桐谷 透",
        ],
        expected={
            "same_vacancy_id": vacancy_id,
            "status": "filled",
            "fill_route": "external",
            "filled_external_name": "桐谷 透",
            "posting_status": "closed",
            "unfilled": 0,
            "short": 0,
        },
        actual={
            "rejected_unqualified": rejected,
            "accepted": accepted,
            "vacancy": vacancy,
            "metrics": snap["metrics"],
            "coverage": snap["coverage"][day],
        },
    )


def test_no_internal_candidate_is_infeasible_and_not_unknown(tmp_path: Path):
    problem = _problem(
        (
            Staff("a", "架空A", leader=True, night_ok=True),
            Staff("b", "架空B", leader=True, night_ok=False),
            Staff("c", "架空C", leader=False, night_ok=False),
        ),
        1,
        {"D": 1, "E": 0, "N": 1},
    )
    service = _service(tmp_path, problem)
    staff_id, day = _working(service.snapshot(), "N")
    assert staff_id == "a"
    service.open_vacancy(staff_id, day)
    vacancy = service.snapshot()["vacancies"][0]
    assert vacancy["solver_code"] == "INFEASIBLE"
    assert vacancy["solver_code"] != "UNKNOWN"
    assert vacancy["offers"] == []
    assert vacancy["can_admin"] is True
    assert "解なし" in vacancy["solver_note"]


def test_concurrent_accept_cannot_fill_twice(tmp_path: Path):
    service = _service(tmp_path, _problem(_leaders((True, True, True)), 1, {"D": 1, "E": 0, "N": 0}))
    staff_id, day = _working(service.snapshot(), "D")
    vacancy_id = service.open_vacancy(staff_id, day)
    offers = service.snapshot()["vacancies"][0]["offers"]
    assert len(offers) >= 2
    first, second = offers[0]["staff_id"], offers[1]["staff_id"]
    service.ask(vacancy_id, first)
    service.ask(vacancy_id, second)
    barrier = threading.Barrier(2)
    results: list[tuple[str, str]] = []

    def accept(sid: str) -> None:
        barrier.wait()
        try:
            service.respond(vacancy_id, sid, "accept")
            results.append((sid, "ok"))
        except ServiceError as exc:
            results.append((sid, f"{exc.status}:{exc.message}"))

    threads = [threading.Thread(target=accept, args=(sid,)) for sid in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(item[1] == "ok" for item in results) == [False, True]
    snap = service.snapshot()
    assert snap["vacancies"][0]["status"] == "filled"
    assert snap["coverage"][day]["shifts"][0]["have"] == 1
    _baseline, assignments = _tables(service)
    assert coverage_errors(service.problem, assignments) == []
    winners = [sid for sid, outcome in results if outcome == "ok"]
    record(
        "concurrent_accept_one",
        inputs={
            "vacancy_id": vacancy_id,
            "absent": {"staff_id": staff_id, "day": day},
            "contenders": [first, second],
        },
        operations=[
            f"ask both {first} and {second}",
            "two threads call respond accept after a barrier",
        ],
        expected={"success_count": 1, "shift_have": 1, "status": "filled", "coverage_errors": []},
        actual={
            "thread_results": results,
            "winners": winners,
            "vacancy": snap["vacancies"][0],
            "coverage": snap["coverage"][day],
            "assignments": {f"{sid}:{d}": shift for (sid, d), shift in assignments.items()},
        },
    )


def test_reload_keeps_open_vacancy(tmp_path: Path):
    path = tmp_path / "ward.sqlite"
    problem = _problem(_leaders((True, True, True)), 1, {"D": 1, "E": 0, "N": 0})
    first = Service(str(path), problem)
    first.init()
    staff_id, day = _working(first.snapshot(), "D")
    vacancy_id = first.open_vacancy(staff_id, day)
    second = Service(str(path))
    second.init()
    snap = second.snapshot()
    assert snap["vacancies"][0]["id"] == vacancy_id
    assert snap["vacancies"][0]["status"] == "open"
    assert snap["metrics"]["unfilled"] == 1
    assert snap["facility"] == problem.facility


def test_factory_uses_database_env(tmp_path: Path, monkeypatch):
    target = tmp_path / "from-env.sqlite"
    monkeypatch.setenv("NURSE_SHIFT_DB", str(target))
    app = create_app(problem=_problem(_leaders((True, True, True)), 1, {"D": 1, "E": 0, "N": 0}))
    assert app.state.service.db_path == str(target)
    assert target.exists()


def test_http_page_states_fictional_data_and_keeps_state(tmp_path: Path):
    db_path = tmp_path / "http.sqlite"
    app = create_app(str(db_path), _problem(_leaders((True, True, True)), 1, {"D": 1, "E": 0, "N": 0}))
    client = TestClient(app)
    page = client.get("/")
    assert page.status_code == 200
    assert "架空データ・外部送信なし" in page.text
    assert "最適です" not in page.text
    assert "カイテク" not in page.text
    assert "保証しません" in page.text
    state = client.get("/api/state").json()
    staff_id, day = _working(state, "D")
    opened = client.post("/api/vacancies", json={"staff_id": staff_id, "day": day})
    assert opened.status_code == 200
    vacancy_id = opened.json()["vacancy_id"]
    reloaded = TestClient(create_app(str(db_path)))
    again = reloaded.get("/api/state").json()
    assert again["vacancies"][0]["id"] == vacancy_id
    assert "架空データ・外部送信なし" in reloaded.get("/").text


def test_demo_roster_supports_both_recovery_paths(tmp_path: Path):
    seed = tmp_path / "seed.sqlite"
    Service(str(seed)).init()
    internal = tmp_path / "internal.sqlite"
    shutil.copy(seed, internal)
    service = Service(str(internal))
    service.init()
    vacancy_id, chosen = _open_with_offer(service)
    service.ask(vacancy_id, chosen)
    service.respond(vacancy_id, chosen, "accept")
    filled = service.snapshot()
    assert filled["metrics"]["unfilled"] == 0
    assert filled["vacancies"][0]["fill_route"] == "internal"
    _baseline, assignments = _tables(service)
    assert coverage_errors(service.problem, assignments) == []
    reloaded = Service(str(internal))
    reloaded.init()
    assert reloaded.snapshot()["vacancies"][0]["status"] == "filled"

    external = tmp_path / "external.sqlite"
    shutil.copy(seed, external)
    other = Service(str(external))
    other.init()
    vacancy_id, _chosen = _open_with_offer(other)
    for offer in other.snapshot()["vacancies"][0]["offers"]:
        other.ask(vacancy_id, offer["staff_id"])
        other.respond(vacancy_id, offer["staff_id"], "decline")
    other.confirm_admin(vacancy_id)
    result = other.mock_apply(vacancy_id, "桐谷 透", True, True)
    assert result["accepted"] is True
    snap = other.snapshot()
    assert snap["vacancies"][0]["id"] == vacancy_id
    assert snap["vacancies"][0]["posting"]["status"] == "closed"
    assert snap["metrics"]["unfilled"] == 0
    day = snap["vacancies"][0]["day"]
    code = snap["vacancies"][0]["shift"]
    shift = next(item for item in snap["coverage"][day]["shifts"] if item["code"] == code)
    assert shift["short"] == 0
    assert shift["have"] == shift["need"]


def test_unqualified_applicant_is_rejected(tmp_path: Path):
    problem = _problem(
        (
            Staff("a", "架空A", leader=True, night_ok=True),
            Staff("b", "架空B", leader=True, night_ok=False),
            Staff("c", "架空C", leader=False, night_ok=False),
        ),
        1,
        {"D": 1, "E": 0, "N": 1},
    )
    service = _service(tmp_path, problem)
    staff_id, day = _working(service.snapshot(), "N")
    vacancy_id = service.open_vacancy(staff_id, day)
    opened = service.snapshot()["vacancies"][0]
    service.confirm_admin(vacancy_id)
    rejected = service.mock_apply(vacancy_id, "架空 資格不足", False, False)
    after = service.snapshot()
    night = next(item for item in after["coverage"][day]["shifts"] if item["code"] == "N")
    assert rejected["accepted"] is False
    assert "夜勤" in rejected["reason"]
    assert after["vacancies"][0]["status"] == "open"
    assert night["have"] == 0
    assert night["short"] == 1
    assert after["vacancies"][0]["posting"]["applications"][0]["accepted"] == 0
    record(
        "qualification_reject",
        inputs={
            "demand": problem.demand,
            "absent": {"staff_id": staff_id, "day": day, "shift": "N"},
            "applicant": {"name": "架空 資格不足", "night_ok": False, "leader": False},
            "ineligible_before_apply": opened["ineligible"],
        },
        operations=[
            f"open_vacancy({staff_id}, {day}) on 深夜",
            "confirm_admin",
            "mock_apply night_ok=false",
        ],
        expected={
            "accepted": False,
            "reason_contains": "夜勤",
            "vacancy_status": "open",
            "night_have": 0,
            "night_short": 1,
        },
        actual={
            "apply_result": rejected,
            "vacancy": after["vacancies"][0],
            "night_coverage": night,
            "metrics": after["metrics"],
        },
    )


def test_second_accept_rejects_double_fill(tmp_path: Path):
    service = _service(tmp_path, _problem(_leaders((True, True, True)), 1, {"D": 1, "E": 0, "N": 0}))
    staff_id, day = _working(service.snapshot(), "D")
    vacancy_id = service.open_vacancy(staff_id, day)
    offers = service.snapshot()["vacancies"][0]["offers"]
    assert len(offers) >= 2
    first, second = offers[0]["staff_id"], offers[1]["staff_id"]
    service.ask(vacancy_id, first)
    service.ask(vacancy_id, second)
    service.respond(vacancy_id, first, "accept")
    with pytest.raises(Conflict) as duplicated:
        service.respond(vacancy_id, second, "accept")
    snap = service.snapshot()
    _baseline, assignments = _tables(service)
    on_shift = [sid for (sid, cell_day), shift in assignments.items() if cell_day == day and shift == "D"]
    assert duplicated.value.status == 409
    assert snap["vacancies"][0]["status"] == "filled"
    assert snap["vacancies"][0]["filled_staff_id"] == first
    assert on_shift == [first]
    assert snap["coverage"][day]["shifts"][0]["have"] == 1
    record(
        "double_fill_reject",
        inputs={
            "vacancy_id": vacancy_id,
            "absent": {"staff_id": staff_id, "day": day, "shift": "D"},
            "first_accept": first,
            "second_accept": second,
        },
        operations=[
            f"ask {first} and {second}",
            f"respond accept {first}",
            f"respond accept {second} after the vacancy is filled",
        ],
        expected={"second_status": 409, "people_on_D": [first], "have": 1, "status": "filled"},
        actual={
            "second_status": duplicated.value.status,
            "second_message": duplicated.value.message,
            "people_on_D": on_shift,
            "vacancy": snap["vacancies"][0],
            "coverage": snap["coverage"][day],
        },
    )


def test_new_process_keeps_confirmed_fill(tmp_path: Path):
    path = tmp_path / "kept.sqlite"
    service = Service(str(path), _problem(_leaders((True, True, True)), 1, {"D": 1, "E": 0, "N": 0}))
    service.init()
    staff_id, day = _working(service.snapshot(), "D")
    vacancy_id = service.open_vacancy(staff_id, day)
    chosen = service.snapshot()["vacancies"][0]["offers"][0]["staff_id"]
    service.ask(vacancy_id, chosen)
    service.respond(vacancy_id, chosen, "accept")
    script = (
        "import json,sys\n"
        "from nurse_shift.service import Service\n"
        "service = Service(sys.argv[1])\n"
        "service.init()\n"
        "print(json.dumps(service.snapshot(), ensure_ascii=False))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    reloaded = json.loads(proc.stdout)
    vacancy = reloaded["vacancies"][0]
    assert vacancy["id"] == vacancy_id
    assert vacancy["status"] == "filled"
    assert vacancy["fill_route"] == "internal"
    assert vacancy["filled_staff_id"] == chosen
    assert reloaded["metrics"]["unfilled"] == 0
    record(
        "restart_keeps_confirmed",
        inputs={
            "database": str(path),
            "vacancy_id": vacancy_id,
            "filled_staff_id": chosen,
        },
        operations=[
            "fill vacancy in the parent process",
            "start a new Python process and Service.init on the same sqlite file",
        ],
        expected={
            "id": vacancy_id,
            "status": "filled",
            "fill_route": "internal",
            "filled_staff_id": chosen,
            "unfilled": 0,
        },
        actual={
            "returncode": proc.returncode,
            "vacancy": vacancy,
            "metrics": reloaded["metrics"],
            "assignments": [
                {"staff_id": person["id"], "cells": person["cells"]} for person in reloaded["staff"]
            ],
        },
    )


def test_rest_rejection_uses_computed_gap(tmp_path: Path):
    import inspect

    from nurse_shift.domain import placement_rest_issue, shift_window

    assert "7.5" not in inspect.getsource(placement_rest_issue)
    problem = Problem(
        facility="架空病棟",
        start_date="2026-11-16",
        staff=(
            Staff("a", "架空A", leader=True, night_ok=False),
            Staff("b", "架空B", leader=True, night_ok=True),
            Staff("c", "架空C", leader=False, night_ok=False),
        ),
        days=2,
        demand={"D": 1, "E": 1, "N": 0},
        preferences=frozenset(),
    )
    service = _service(tmp_path, problem)
    holder = next(person["id"] for person in service.snapshot()["staff"] if person["cells"][1]["shift"] == "D")
    service.open_vacancy(holder, 1)
    vacancy = service.snapshot()["vacancies"][0]
    rejected = next(item for item in vacancy["ineligible"] if item["staff_id"] == "b")
    end_of_evening = shift_window(0, "E")[1]
    start_of_day = shift_window(1, "D")[0]
    hours = (start_of_day - end_of_evening) / 60
    assert f"{hours:.1f}" in rejected["reason"]
    assert "準夜の終了から日勤の開始まで" in rejected["reason"]
    assert "架空の設定" in rejected["reason"]
    assert "ありません求解" not in rejected["reason"]
    record(
        "rest_rejection_computed_gap",
        inputs={"absent": holder, "day": 1, "shift": "D", "rejected_staff": "b", "rest_minutes": 660},
        operations=["open day-1 日勤 vacancy", "read ineligible reason for the only night-capable nurse"],
        expected={"hours": hours, "mentions_fictional_threshold": True},
        actual={"reason": rejected["reason"], "solver_code": rejected["solver_code"]},
    )


def test_offer_reason_separates_sentences(tmp_path: Path):
    service = _service(tmp_path, _problem(_leaders((True, True, True)), 1, {"D": 1, "E": 0, "N": 0}))
    staff_id, day = _working(service.snapshot(), "D")
    service.open_vacancy(staff_id, day)
    reason = service.snapshot()["vacancies"][0]["offers"][0]["reason"]
    assert "ありません。求解状態" in reason or "です。求解状態" in reason
    assert "ありません求解" not in reason
    assert "です求解" not in reason
    record(
        "reason_punctuation",
        inputs={"absent": staff_id, "day": day},
        operations=["open vacancy and read the first eligible reason"],
        expected={"has_period_before_solver_status": True},
        actual={"reason": reason},
    )


def test_recommendation_order_prefers_fewer_violations_at_same_changes(tmp_path: Path):
    problem = _problem(_leaders((True, True, True)), 1, {"D": 1, "E": 0, "N": 0})
    service = _service(tmp_path, problem)
    staff_id, day = _working(service.snapshot(), "D")
    service.open_vacancy(staff_id, day)
    offers = service.snapshot()["vacancies"][0]["offers"]
    keys = [(item["change_count"], item["preference_violations"], item["staff_id"]) for item in offers]
    assert keys == sorted(keys)
    app = create_app(str(tmp_path / "view.sqlite"), problem)
    client = TestClient(app)
    viewed = _working(client.get("/api/state").json(), "D")
    client.post("/api/vacancies", json={"staff_id": viewed[0], "day": viewed[1]})
    page = client.get("/")
    assert "推薦順" in page.text
    assert "最適ではありません" in page.text
    assert "先に「入れる」" in page.text
    record(
        "recommendation_versus_acceptance",
        inputs={"preference": [["a", 0]], "demand": {"D": 1}, "absent": staff_id},
        operations=[
            "list eligible offers",
            "sort key is change_count, preference_violations, staff_id",
            "acceptance stays a human response, not an automatic pick",
        ],
        expected={"sorted": True, "explains_acceptance_order": True},
        actual={"offer_keys": keys},
    )


def _open_with_offer(service: Service) -> tuple[int, str]:
    snap = service.snapshot()
    for person in snap["staff"]:
        for cell in person["cells"]:
            if cell["shift"] == "OFF":
                continue
            trial = service.db_path + ".trial"
            shutil.copy(service.db_path, trial)
            probe = Service(trial)
            probe.init()
            try:
                vacancy_id = probe.open_vacancy(person["id"], cell["day"])
            except ServiceError:
                continue
            offers = probe.snapshot()["vacancies"][0]["offers"]
            if not offers:
                continue
            kept = service.open_vacancy(person["id"], cell["day"])
            chosen = service.snapshot()["vacancies"][0]["offers"][0]["staff_id"]
            assert kept
            return kept, chosen
    raise AssertionError("demo roster produced no internal candidate")


def _tables(service: Service):
    from nurse_shift.db import connect

    conn = connect(service.db_path)
    try:
        return service._baseline(conn), service._assignments(conn)
    finally:
        conn.close()


def test_filler_absent_same_day_does_not_lock_solver(tmp_path: Path):
    """代わりに入った人が同じ日にさらに欠勤しても、矛盾制約で以降が全滅しない。"""
    service = Service(str(tmp_path / "ward.sqlite"))
    service.init()
    snap = service.snapshot()
    first_absent = next(p["id"] for p in snap["staff"] if p["cells"][2]["shift"] == "D")
    v1 = service.open_vacancy(first_absent, 2)
    filler = service.snapshot()["vacancies"][0]["offers"][0]["staff_id"]
    service.ask(v1, filler)
    service.respond(v1, filler, "accept")

    v2 = service.open_vacancy(filler, 2)
    second = service.snapshot()["vacancies"][1]
    assert second["solver_code"] in ("OPTIMAL", "FEASIBLE")
    assert second["offers"], "代替者の再欠勤でも内部候補が出る"
    replacement = second["offers"][0]["staff_id"]
    assert replacement not in (first_absent, filler)
    service.ask(v2, replacement)
    service.respond(v2, replacement, "accept")

    _baseline, assignments = _tables(service)
    assert assignments[(first_absent, 2)] == "OFF"
    assert assignments[(filler, 2)] == "OFF"
    assert assignments[(replacement, 2)] == "D"
    assert coverage_errors(service.problem, assignments) == []

    later = next(
        p["id"] for p in service.snapshot()["staff"] if p["cells"][6]["shift"] == "D"
    )
    service.open_vacancy(later, 6)
    third = service.snapshot()["vacancies"][2]
    assert third["solver_code"] in ("OPTIMAL", "FEASIBLE")
    assert third["offers"]


def test_missing_or_bad_input_returns_400_not_500(tmp_path: Path):
    client = TestClient(create_app(str(tmp_path / "bad.sqlite")), raise_server_exceptions=False)
    cases = [
        ("/api/vacancies", {"day": 1}),
        ("/api/vacancies", {"staff_id": "n01", "day": "x"}),
        ("/api/vacancies/1/ask", {}),
        ("/api/vacancies/1/respond", {"staff_id": "n01"}),
    ]
    for url, body in cases:
        response = client.post(url, json=body)
        assert response.status_code == 400, (url, body, response.text)
        assert "error" in response.json()
    broken = client.post("/api/vacancies", content=b"{", headers={"content-type": "application/json"})
    assert broken.status_code == 400
    form = client.post("/api/vacancies", data={}, follow_redirects=False)
    assert form.status_code == 303 and "error=" in form.headers["location"]
    assert client.get("/api/state").json()["vacancies"] == []
