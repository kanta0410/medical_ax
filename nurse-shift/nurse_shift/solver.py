"""OR-Tools CP-SAT で初期勤務と欠員の再割当を解く。"""

from __future__ import annotations

from dataclasses import dataclass
import time

from ortools.sat.python import cp_model

from nurse_shift.domain import SHIFTS, Problem, measure, pair_issue


@dataclass
class SolveResult:
    code: str
    assignments: dict[tuple[str, int], str] | None
    change_count: int | None
    preference_violations: int | None
    solve_ms: int


def solve(
    problem: Problem,
    *,
    baseline: dict[tuple[str, int], str] | None = None,
    force_off: list[tuple[str, int]] | None = None,
    force_on: list[tuple[str, int, str]] | None = None,
    external: dict[tuple[int, str], int] | None = None,
    external_leaders: dict[int, int] | None = None,
    time_limit_s: float = 5.0,
) -> SolveResult:
    started = time.perf_counter()
    model = cp_model.CpModel()
    staff_ids = [s.id for s in problem.staff]
    night = {s.id for s in problem.staff if s.night_ok}
    leaders = {s.id for s in problem.staff if s.leader}
    x: dict[tuple[str, int, str], cp_model.IntVar] = {}
    for sid in staff_ids:
        for day in range(problem.days):
            for shift in SHIFTS:
                x[sid, day, shift] = model.new_bool_var(f"x_{sid}_{day}_{shift}")
            model.add(sum(x[sid, day, shift] for shift in SHIFTS) <= 1)
            if sid not in night:
                model.add(x[sid, day, "E"] == 0)
                model.add(x[sid, day, "N"] == 0)

    external = external or {}
    external_leaders = external_leaders or {}
    for day in range(problem.days):
        for shift, need in problem.demand.items():
            covered = external.get((day, shift), 0)
            model.add(sum(x[sid, day, shift] for sid in staff_ids) + covered == need)
        if problem.demand.get("D", 0) > 0:
            model.add(
                sum(x[sid, day, "D"] for sid in leaders)
                + external_leaders.get(day, 0)
                >= 1
            )

    for sid in staff_ids:
        for day in range(problem.days - 1):
            for left in SHIFTS:
                for right in SHIFTS:
                    if pair_issue(day, left, day + 1, right, problem.rest_minutes):
                        model.add(x[sid, day, left] + x[sid, day + 1, right] <= 1)

    for sid, day in force_off or []:
        for shift in SHIFTS:
            model.add(x[sid, day, shift] == 0)
    for sid, day, shift in force_on or []:
        model.add(x[sid, day, shift] == 1)

    known = set(staff_ids)
    pref_terms = []
    for sid, day in problem.preferences:
        if sid not in known or day < 0 or day >= problem.days:
            continue
        worked = model.new_bool_var(f"pref_{sid}_{day}")
        model.add(sum(x[sid, day, shift] for shift in SHIFTS) == worked)
        pref_terms.append(worked)

    if baseline is None:
        night_counts = []
        for sid in staff_ids:
            if sid not in night:
                continue
            count = sum(x[sid, day, "E"] + x[sid, day, "N"] for day in range(problem.days))
            night_counts.append(count)
        if night_counts:
            upper = problem.days * 2
            max_n = model.new_int_var(0, upper, "max_nights")
            min_n = model.new_int_var(0, upper, "min_nights")
            for count in night_counts:
                model.add(max_n >= count)
                model.add(min_n <= count)
            spread = max_n - min_n
        else:
            spread = 0
        model.minimize(spread * 10 + sum(pref_terms))
    else:
        changes = []
        for sid in staff_ids:
            for day in range(problem.days):
                current = baseline.get((sid, day), "OFF")
                changed = model.new_bool_var(f"ch_{sid}_{day}")
                if current == "OFF":
                    model.add(sum(x[sid, day, shift] for shift in SHIFTS) == changed)
                else:
                    model.add(x[sid, day, current] + changed == 1)
                changes.append(changed)
        model.minimize(sum(changes) * 1000 + sum(pref_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 1
    status = solver.solve(model)
    code = solver.status_name(status)
    elapsed = int((time.perf_counter() - started) * 1000)
    if code not in ("OPTIMAL", "FEASIBLE"):
        return SolveResult(code, None, None, None, elapsed)

    assignments: dict[tuple[str, int], str] = {}
    for sid in staff_ids:
        for day in range(problem.days):
            chosen = "OFF"
            for shift in SHIFTS:
                if solver.value(x[sid, day, shift]) == 1:
                    chosen = shift
                    break
            assignments[sid, day] = chosen
    reference = baseline if baseline is not None else assignments
    measured = measure(problem, reference, assignments)
    return SolveResult(
        code=code,
        assignments=assignments,
        change_count=measured["changed_cells"],
        preference_violations=measured["preference_violations"],
        solve_ms=elapsed,
    )
