"""起動中の画面で、内部充足と架空応募の両方を通す。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = Path(os.environ.get("NURSE_SHIFT_DEMO_DB", "/tmp/nurse-shift-demo.sqlite"))
PORT = int(os.environ.get("NURSE_SHIFT_DEMO_PORT", "8765"))
BASE = f"http://127.0.0.1:{PORT}"
OUT = Path(os.environ.get("NURSE_SHIFT_DEMO_OUT", str(ROOT / "demo-artifacts")))
CHROME = os.environ.get("NURSE_SHIFT_CHROME", "/usr/bin/google-chrome")


def wait_health() -> None:
    for _ in range(50):
        try:
            with urllib.request.urlopen(BASE + "/api/health", timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("server did not start")


def start_server() -> subprocess.Popen:
    if DB.exists():
        DB.unlink()
    env = os.environ.copy()
    env["NURSE_SHIFT_DB"] = str(DB)
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "nurse_shift.main:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORT),
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def restart(proc: subprocess.Popen) -> subprocess.Popen:
    proc.terminate()
    proc.wait(timeout=10)
    env = os.environ.copy()
    env["NURSE_SHIFT_DB"] = str(DB)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "nurse_shift.main:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORT),
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    wait_health()
    return proc


def state() -> dict:
    with urllib.request.urlopen(BASE + "/api/state", timeout=10) as response:
        return json.loads(response.read().decode())


def main() -> None:
    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    log = []
    proc = start_server()
    try:
        wait_health()
        with sync_playwright() as playwright:
            launch_kwargs = {
                "headless": True,
                "args": ["--no-sandbox", "--disable-dev-shm-usage"],
            }
            if Path(CHROME).exists():
                launch_kwargs["executable_path"] = CHROME
            browser = playwright.chromium.launch(**launch_kwargs)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(BASE, wait_until="networkidle")
            page.screenshot(path=str(OUT / "nurse-shift-board.png"), full_page=True)
            log.append({"step": "board", "metrics": state()["metrics"], "roster": state()["roster"]["code"]})

            page.get_by_role("button", name="この勤務を欠勤にする").first.click()
            page.wait_for_selector("article.vacancy")
            opened = state()
            vacancy_id = opened["vacancies"][0]["id"]
            page.screenshot(path=str(OUT / "nurse-shift-shortage.png"), full_page=True)
            log.append(
                {
                    "step": "shortage",
                    "vacancy": vacancy_id,
                    "solver": opened["vacancies"][0]["solver_code"],
                    "short": opened["vacancies"][0]["short"],
                    "offers": len(opened["vacancies"][0]["offers"]),
                    "metrics": opened["metrics"],
                }
            )

            proc = restart(proc)
            page.goto(BASE, wait_until="networkidle")
            reloaded = state()
            assert reloaded["vacancies"][0]["id"] == vacancy_id
            assert reloaded["vacancies"][0]["status"] == "open"
            page.screenshot(path=str(OUT / "nurse-shift-reloaded.png"), full_page=True)
            log.append({"step": "reloaded", "vacancy": reloaded["vacancies"][0]["id"], "metrics": reloaded["metrics"]})

            page.get_by_role("button", name="打診する（送信しません）").first.click()
            page.get_by_role("button", name="入れる（再検証して確定）").click()
            page.wait_for_selector("text=充足（内部）")
            internal = state()
            page.screenshot(path=str(OUT / "nurse-shift-internal-filled.png"), full_page=True)
            log.append(
                {
                    "step": "internal_filled",
                    "route": internal["vacancies"][0]["fill_route"],
                    "commit": internal["vacancies"][0]["commit_code"],
                    "metrics": internal["metrics"],
                }
            )

            page.get_by_role("button", name="架空の勤務表を計算し直す").click()
            page.wait_for_selector("text=この勤務を欠勤にする")
            page.get_by_role("button", name="この勤務を欠勤にする").first.click()
            page.wait_for_selector("article.vacancy")
            while page.get_by_role("button", name="打診する（送信しません）").count():
                page.get_by_role("button", name="打診する（送信しません）").first.click()
                page.get_by_role("button", name="無理").first.click()
            page.get_by_role("button", name="管理者として、この欠員の募集シミュレーションを開く").click()
            if page.get_by_role("button", name="資格不足の応募を試す").count():
                page.get_by_role("button", name="資格不足の応募を試す").click()
                page.wait_for_selector(".alert")
                log.append({"step": "unqualified", "alert": page.locator(".alert").inner_text()})
            page.get_by_role("button", name="架空の応募を検証する").click()
            page.wait_for_selector("text=充足（架空応募")
            external = state()
            page.screenshot(path=str(OUT / "nurse-shift-external-filled.png"), full_page=True)
            log.append(
                {
                    "step": "external_filled",
                    "vacancy": external["vacancies"][0]["id"],
                    "route": external["vacancies"][0]["fill_route"],
                    "name": external["vacancies"][0]["filled_external_name"],
                    "posting": external["vacancies"][0]["posting"]["status"],
                    "metrics": external["metrics"],
                }
            )
            browser.close()
        (OUT / "demo-flow.log").write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(log, ensure_ascii=False, indent=2))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
