"""画面証拠用: 解なし / 日跨ぎ休息不採用 の架空病棟を起動する。"""

from __future__ import annotations

from nurse_shift.domain import Problem, Staff
from nurse_shift.main import create_app


def create_infeasible_app():
    """内部候補ゼロ（夜勤欠員で night_ok が欠勤者のみ）→ INFEASIBLE。"""
    problem = Problem(
        facility="架空病棟（解なし検証用）",
        start_date="2026-11-16",
        staff=(
            Staff("a", "架空A", leader=True, night_ok=True),
            Staff("b", "架空B", leader=True, night_ok=False),
            Staff("c", "架空C", leader=False, night_ok=False),
        ),
        days=1,
        demand={"D": 1, "E": 0, "N": 1},
        preferences=frozenset(),
    )
    return create_app(problem=problem)


def create_rest_app():
    """準夜→翌日日勤の休息不足（7.5時間）が不採用理由に出る架空病棟。"""
    problem = Problem(
        facility="架空病棟（日跨ぎ休息検証用）",
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
    return create_app(problem=problem)
