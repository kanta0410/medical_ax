"""Dockerfile の CMD と同じ起動方法で本物の HTTP サーバーを立て、外部送信なしで確認する。

- PORT / NURSE_SHIFT_DB 環境変数で起動できる（Cloud Run 相当）
- /api/health と / が 200
- 2 人に打診し、HTTP で同時に「入れる」を送っても充足は 1 件だけ
- サーバー再起動後も充足が残る（同じ DB ファイル）
架空データのみ。イメージ作成・デプロイ・課金はしない。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = int(os.environ.get("SMOKE_PORT", "8791"))
BASE = f"http://127.0.0.1:{PORT}"


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict | str]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if body is not None:
        req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            raw = res.read().decode()
            status = res.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        status = exc.code
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def start(db: str) -> subprocess.Popen:
    env = dict(os.environ, PORT=str(PORT), NURSE_SHIFT_DB=db)
    # Dockerfile の CMD と同じ形（host 0.0.0.0 ではなくローカル検証用に 127.0.0.1）
    cmd = f"exec {sys.executable} -m uvicorn nurse_shift.main:create_app --factory --host 127.0.0.1 --port ${{PORT}}"
    proc = subprocess.Popen(["sh", "-c", cmd], cwd=ROOT, env=env)
    for _ in range(120):
        try:
            if call("GET", "/api/health")[0] == 200:
                return proc
        except OSError:
            pass
        time.sleep(0.25)
    proc.terminate()
    raise SystemExit("server did not start")


def main() -> int:
    log: list[str] = []
    db = os.path.join(tempfile.mkdtemp(), "nurse-shift.sqlite")
    proc = start(db)
    try:
        assert call("GET", "/api/health") == (200, {"ok": True})
        status, html = call("GET", "/")
        assert status == 200 and "架空データ・外部送信なし" in html
        log.append("health 200 / index 200 with fictional-data banner")
        snap = call("GET", "/api/state")[1]
        target = next(
            (p["id"], c["day"]) for p in snap["staff"] for c in p["cells"] if c["shift"] == "D"
        )
        status, snap = call("POST", "/api/vacancies", {"staff_id": target[0], "day": target[1]})
        assert status == 200, snap
        vid = snap["vacancy_id"]
        vac = next(v for v in snap["vacancies"] if v["id"] == vid)
        offers = [o["staff_id"] for o in vac["offers"]][:2]
        assert len(offers) == 2, vac
        for sid in offers:
            assert call("POST", f"/api/vacancies/{vid}/ask", {"staff_id": sid})[0] == 200
        barrier = threading.Barrier(2)
        results: list[int] = []

        def accept(sid: str) -> None:
            barrier.wait()
            results.append(call("POST", f"/api/vacancies/{vid}/respond", {"staff_id": sid, "decision": "accept"})[0])

        threads = [threading.Thread(target=accept, args=(s,)) for s in offers]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert sorted(results)[0] == 200 and sorted(results)[1] >= 400, results
        vac = next(v for v in call("GET", "/api/state")[1]["vacancies"] if v["id"] == vid)
        accepted = [o for o in vac["offers"] if o["response"] == "accepted"]
        assert vac["status"] == "filled" and len(accepted) == 1, vac
        log.append(f"concurrent HTTP accept statuses={sorted(results)} -> filled, accepted=1")
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    proc = start(db)
    try:
        vac = next(v for v in call("GET", "/api/state")[1]["vacancies"] if v["id"] == vid)
        assert vac["status"] == "filled"
        log.append("restart with same NURSE_SHIFT_DB keeps filled vacancy")
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    print("\n".join(["CONTAINER SMOKE OK", *log]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
