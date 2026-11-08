"""解なし(INFEASIBLE)・同時承諾(409)・日跨ぎ休息不採用(7.5h)の画面証拠を残す。

既存 scripts/run_demo.py（ハッピーパス）は変更しない。
環境変数:
  NURSE_SHIFT_DEMO_OUT  成果物ディレクトリ（既定: <root>/demo-artifacts）
  NURSE_SHIFT_DEMO_PORT 既定 8777
  NURSE_SHIFT_CHROME    Chrome 実行ファイル
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(os.environ.get("NURSE_SHIFT_DEMO_OUT", str(ROOT / "demo-artifacts")))
CHROME = os.environ.get("NURSE_SHIFT_CHROME", "/usr/bin/google-chrome")
PORT = int(os.environ.get("NURSE_SHIFT_DEMO_PORT", "8777"))
BASE = f"http://127.0.0.1:{PORT}"


def start(factory: str, db: Path) -> subprocess.Popen:
    if db.exists():
        db.unlink()
    env = os.environ.copy()
    env["NURSE_SHIFT_DB"] = str(db)
    env["PYTHONPATH"] = str(ROOT)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", factory, "--factory", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    for _ in range(60):
        try:
            with urllib.request.urlopen(BASE + "/api/health", timeout=1) as response:
                if response.status == 200:
                    return proc
        except Exception:
            time.sleep(0.2)
    out = proc.stdout.read() if proc.stdout else ""
    raise RuntimeError(f"server did not start\n{out}")


def stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def state() -> dict:
    with urllib.request.urlopen(BASE + "/api/state", timeout=10) as response:
        return json.loads(response.read().decode())


def launch_browser(playwright):
    kwargs = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
    if Path(CHROME).exists():
        kwargs["executable_path"] = CHROME
    return playwright.chromium.launch(**kwargs)


def case_concurrent(browser, log: list[dict]) -> None:
    """二人に打診→先勝ち充足→古い画面の二人目は 409 相当アラート。"""
    proc = start("nurse_shift.main:create_app", Path("/tmp/nurse-shift-edge-concurrent.sqlite"))
    try:
        a = browser.new_page(viewport={"width": 1440, "height": 1000})
        a.goto(BASE, wait_until="networkidle")
        a.get_by_role("button", name="この勤務を欠勤にする").first.click()
        a.wait_for_selector("article.vacancy")
        for _ in range(2):
            a.get_by_role("button", name="打診する（送信しません）").first.click()
            a.wait_for_load_state("networkidle")
        asked = [o for o in state()["vacancies"][0]["offers"] if o["response"] == "asked"]
        assert len(asked) == 2, asked

        b = browser.new_page(viewport={"width": 1440, "height": 1000})
        b.goto(BASE, wait_until="networkidle")
        a.get_by_role("button", name="入れる（再検証して確定）").first.click()
        a.wait_for_selector("text=充足（内部）")
        a.screenshot(path=str(OUT / "edge-concurrent-first-accepted.png"), full_page=True)

        b.get_by_role("button", name="入れる（再検証して確定）").nth(1).click()
        b.wait_for_selector(".alert")
        alert = b.locator(".alert").inner_text()
        b.screenshot(path=str(OUT / "edge-concurrent-second-rejected.png"), full_page=True)
        snap = state()
        vacancy = snap["vacancies"][0]
        accepted = [o["staff_id"] for o in vacancy["offers"] if o["response"] == "accepted"]
        assert vacancy["status"] == "filled" and len(accepted) == 1, vacancy
        assert "既に充足" in alert
        log.append(
            {
                "step": "concurrent_accept",
                "asked": [o["staff_id"] for o in asked],
                "accepted": accepted,
                "status": vacancy["status"],
                "second_alert": alert,
                "http_conflict": 409,
                "metrics": snap["metrics"],
                "shots": [
                    "edge-concurrent-first-accepted.png",
                    "edge-concurrent-second-rejected.png",
                ],
            }
        )
    finally:
        stop(proc)


def case_infeasible(browser, log: list[dict]) -> None:
    """夜勤欠員で内部候補ゼロ → INFEASIBLE + 管理者確認のみ。"""
    proc = start("scripts.edge_app:create_infeasible_app", Path("/tmp/nurse-shift-edge-infeasible.sqlite"))
    try:
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(BASE, wait_until="networkidle")
        snap = state()
        target = None
        for person in snap["staff"]:
            for cell in person["cells"]:
                if cell["shift"] == "N":
                    target = person["id"]
        assert target == "a", target
        page.locator("form.cell-form", has=page.locator("input[name=staff_id][value=a]")).get_by_role(
            "button", name="この勤務を欠勤にする"
        ).click()
        page.wait_for_selector("article.vacancy")
        vacancy = state()["vacancies"][0]
        assert vacancy["solver_code"] == "INFEASIBLE" and vacancy["offers"] == [] and vacancy["can_admin"]
        assert page.get_by_role("button", name="打診する（送信しません）").count() == 0
        assert "解なし" in page.locator("article.vacancy").inner_text()
        page.screenshot(path=str(OUT / "edge-infeasible.png"), full_page=True)
        log.append(
            {
                "step": "infeasible",
                "solver": vacancy["solver_code"],
                "note": vacancy["solver_note"],
                "offers": len(vacancy["offers"]),
                "can_admin": vacancy["can_admin"],
                "shots": ["edge-infeasible.png"],
            }
        )
    finally:
        stop(proc)


def case_overnight_rest(browser, log: list[dict]) -> None:
    """day1 日勤欠員で、準夜明け候補の不採用理由に 7.5 時間が画面表示される。"""
    proc = start("scripts.edge_app:create_rest_app", Path("/tmp/nurse-shift-edge-rest.sqlite"))
    try:
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(BASE, wait_until="networkidle")
        snap = state()
        holder = next(
            person["id"] for person in snap["staff"] if person["cells"][1]["shift"] == "D"
        )
        page.locator(
            "form.cell-form",
            has=page.locator(f"input[name=staff_id][value={holder}]"),
        ).filter(has=page.locator('input[name=day][value="1"]')).get_by_role(
            "button", name="この勤務を欠勤にする"
        ).click()
        page.wait_for_selector("article.vacancy")
        vacancy = state()["vacancies"][0]
        rejected = next(item for item in vacancy["ineligible"] if item["staff_id"] == "b")
        assert "7.5" in rejected["reason"], rejected["reason"]
        assert "準夜の終了から日勤の開始まで" in rejected["reason"]

        details = page.locator("article.vacancy details").first
        details.locator("summary").click()
        page.wait_for_selector("article.vacancy details[open]")
        body = page.locator("article.vacancy").inner_text()
        assert "7.5" in body, body
        page.locator("article.vacancy details").first.scroll_into_view_if_needed()
        page.screenshot(path=str(OUT / "edge-overnight-rest-7_5h.png"), full_page=True)
        # カード付近のクローズアップ（理由行を読みやすく）
        page.locator("article.vacancy").screenshot(path=str(OUT / "edge-overnight-rest-card.png"))
        log.append(
            {
                "step": "overnight_rest",
                "holder": holder,
                "day": 1,
                "shift": vacancy["shift"],
                "rejected_staff": "b",
                "reason": rejected["reason"],
                "shots": ["edge-overnight-rest-7_5h.png", "edge-overnight-rest-card.png"],
            }
        )
    finally:
        stop(proc)


def main() -> None:
    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    log: list[dict] = []
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        case_concurrent(browser, log)
        case_infeasible(browser, log)
        case_overnight_rest(browser, log)
        browser.close()
    (OUT / "demo-flow.log").write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(log, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
