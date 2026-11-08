from nurse_shift.domain import (
    SHIFTS,
    Staff,
    Problem,
    coverage_errors,
    pair_issue,
    status_note,
)
from nurse_shift.solver import solve
from tests.record import record


def _rest_problem() -> Problem:
    staff = tuple(
        Staff(sid, f"架空{sid}", leader=True, night_ok=True) for sid in ("a", "b", "c", "d")
    )
    return Problem(
        facility="架空病棟",
        start_date="2026-11-16",
        staff=staff,
        days=2,
        demand={"D": 1, "E": 1, "N": 0},
        preferences=frozenset(),
    )


def test_overnight_junya_into_day_shift_is_short_rest():
    issue = pair_issue(0, "E", 1, "D", 11 * 60)
    assert issue is not None
    assert "7.5" in issue
    assert "11" in issue
    record(
        "overnight_junya_to_day",
        inputs={"left": "day0 準夜 16:30-翌1:00", "right": "day1 日勤 8:30-17:00", "rest_minutes": 660},
        operations=["pair_issue(0, E, 1, D, 660)"],
        expected={"blocked": True, "gap_hours": 7.5, "threshold_hours": 11},
        actual={"issue": issue},
    )


def test_overnight_junya_then_next_junya_meets_demo_rest():
    issue = pair_issue(0, "E", 1, "E", 11 * 60)
    assert issue is None
    record(
        "overnight_junya_to_next_junya",
        inputs={"left": "day0 準夜 終了 翌1:00", "right": "day1 準夜 開始 16:30", "rest_minutes": 660},
        operations=["pair_issue(0, E, 1, E, 660)"],
        expected={"issue": None, "gap_hours": 15.5},
        actual={"issue": issue},
    )


def test_junya_overlaps_next_midnight_shift():
    issue = pair_issue(0, "E", 1, "N", 11 * 60)
    assert issue is not None
    assert issue.startswith("重複")
    record(
        "overnight_junya_overlaps_night",
        inputs={"left": "day0 準夜 終了 翌1:00", "right": "day1 深夜 開始 0:30", "rest_minutes": 660},
        operations=["pair_issue(0, E, 1, N, 660)"],
        expected={"starts_with": "重複"},
        actual={"issue": issue},
    )


def test_solver_rejects_fixed_overnight_rest_break_and_accepts_the_boundary():
    problem = _rest_problem()
    blocked = solve(problem, force_on=[("b", 0, "E"), ("b", 1, "D")])
    assert blocked.code == "INFEASIBLE"
    assert blocked.assignments is None
    allowed = solve(problem, force_on=[("b", 0, "E")], force_off=[("b", 1)])
    assert allowed.code in {"OPTIMAL", "FEASIBLE"}
    assert allowed.assignments is not None
    assert allowed.assignments[("b", 0)] == "E"
    assert allowed.assignments[("b", 1)] == "OFF"
    illegal = {
        ("a", 0): "D",
        ("a", 1): "E",
        ("b", 0): "E",
        ("b", 1): "D",
        ("c", 0): "OFF",
        ("c", 1): "OFF",
        ("d", 0): "OFF",
        ("d", 1): "OFF",
    }
    errors = coverage_errors(problem, illegal)
    rest_errors = [item for item in errors if "休息" in item]
    assert rest_errors
    assert "7.5" in rest_errors[0]
    record(
        "overnight_rest_reject",
        inputs={
            "staff": [s.id for s in problem.staff],
            "days": problem.days,
            "demand": problem.demand,
            "rest_minutes": problem.rest_minutes,
            "forced_pair": [["b", 0, "E"], ["b", 1, "D"]],
            "clocks": {"E": "16:30-翌1:00", "D": "8:30-17:00"},
        },
        operations=[
            "pair_issue(day0 準夜, day1 日勤, rest=660)",
            "CP-SAT force_on b: 準夜 then 日勤",
            "coverage_errors on a hand-built roster with that pair",
        ],
        expected={
            "pair_issue_contains": "7.5",
            "solver_code": "INFEASIBLE",
            "solver_assignments": None,
            "validator_mentions_rest": True,
        },
        actual={
            "pair_issue": pair_issue(0, "E", 1, "D", problem.rest_minutes),
            "solver_code": blocked.code,
            "solver_assignments": blocked.assignments,
            "validator_errors": errors,
        },
    )
    for sid in ("a", "b", "c", "d"):
        for day in range(1):
            left = allowed.assignments[(sid, day)]
            right = allowed.assignments[(sid, day + 1)]
            if left in SHIFTS and right in SHIFTS:
                assert pair_issue(day, left, day + 1, right, problem.rest_minutes) is None


def test_status_labels_do_not_call_unproven_feasible_optimal():
    feasible = status_note("FEASIBLE")
    unknown = status_note("UNKNOWN")
    infeasible = status_note("INFEASIBLE")
    optimal = status_note("OPTIMAL")
    assert "未証明" in feasible
    assert "最適です" not in feasible
    assert "最適です" not in optimal
    assert "保証ではありません" in optimal
    assert "解なし" in infeasible
    assert "解なし" in unknown
    assert unknown != infeasible
    assert "INFEASIBLE" in infeasible
    assert "UNKNOWN" in unknown
