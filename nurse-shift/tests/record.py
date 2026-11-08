"""検証ランのときだけ、入力・操作・期待・実測を1行JSONで残す。"""

from __future__ import annotations

import json
import os
from pathlib import Path


def record(case_id: str, *, inputs: dict, operations: list, expected: dict, actual: dict) -> None:
    dest = os.environ.get("VERIFICATION_CASES")
    if not dest:
        return
    path = Path(dest)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "case_id": case_id,
        "inputs": inputs,
        "operations": operations,
        "expected": expected,
        "actual": actual,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
