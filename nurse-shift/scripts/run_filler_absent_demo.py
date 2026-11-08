"""代替者再欠勤の画面デモ。

内部で代わりに入った人が同じ日にさらに欠勤しても、新しい欠員に候補が出て
INFEASIBLE で固まらず、打診→承諾で両方の欠員が充足することを実画面で確かめる。
架空データのみ。外部送信なし。既存の run_demo.py / run_demo_edge.py とは独立。
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
DB = Path(os.environ.get("NURSE_SHIFT_DEMO_DB", "/tmp/nurse-shift-filler-absent-demo.sqlite"))
PORT = int(os.environ.get("NURSE_SHIFT_DEMO_PORT", "8881"))
BASE = f"http://127.0.0.1:{PORT}"
OUT = Path(os.environ.get("NURSE_SHIFT_DEMO_OUT", str(ROOT / "demo-artifacts" / "filler-absent")))
CHROME = os.environ.get("NURSE_SHIFT_CHROME", "/usr/bin/google-chrome")
OK_CODES = ("OPTIMAL", "FEASIBLE")


def wait_health() -> None:
    for _ in range(75):
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
        [sys.executable, "-m", "uvicorn", "nurse_shift.main:create_app", "--factory",
         "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def state() -> dict:
    with urllib.request.urlopen(BASE + "/api/state", timeout=10) as response:
        return json.loads(response.read().decode())


def cell_button(page, staff_id: str, day: int):
    return page.locator(
        f"form.cell-form:has(input[name='staff_id'][value='{staff_id}'])"
        f":has(input[name='day'][value='{day}']) button"
    )


def offer_card(page, vacancy_id: int, name: str):
    return page.locator(f"#vacancy-{vacancy_id} li.offer").filter(has_text=name)


def main() -> None:
    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    log: list[dict] = []
    proc = start_server()
    try:
        wait_health()
        with sync_playwright() as playwright:
            kwargs = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
            if Path(CHROME).exists():
                kwargs["executable_path"] = CHROME
            browser = playwright.chromium.launch(**kwargs)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(BASE, wait_until="networkidle")
            page.screenshot(path=str(OUT / "01-board.png"), full_page=True)

            # 1) 日勤の人を欠勤にする（3日目=day 2。回帰テストと同じ日）
            snap = state()
            day = int(os.environ.get("NURSE_SHIFT_DEMO_DAY", "2"))
            names = {p["id"]: p["name"] for p in snap["staff"]}
            first_absent = next(p["id"] for p in snap["staff"] if p["cells"][day]["shift"] == "D")
            log.append({"step": "board", "day": day, "first_absent": names[first_absent],
                        "metrics": snap["metrics"]})
            cell_button(page, first_absent, day).click()
            page.wait_for_selector("article.vacancy")
            v1 = state()["vacancies"][0]
            assert v1["status"] == "open" and v1["solver_code"] in OK_CODES and v1["offers"], v1
            filler = v1["offers"][0]["staff_id"]
            page.screenshot(path=str(OUT / "02-first-vacancy-candidates.png"), full_page=True)
            log.append({"step": "first_vacancy", "vacancy": v1["id"], "solver": v1["solver_code"],
                        "offers": [o["name"] for o in v1["offers"]]})

            # 2) 先頭候補へ打診→承諾（内部充足）
            offer_card(page, v1["id"], names[filler]).get_by_role("button", name="打診する（送信しません）").click()
            offer_card(page, v1["id"], names[filler]).get_by_role("button", name="入れる（再検証して確定）").click()
            page.wait_for_selector(f"#vacancy-{v1['id']} >> text=充足（内部）")
            after1 = state()
            assert after1["vacancies"][0]["status"] == "filled"
            assert after1["metrics"]["unfilled"] == 0
            filler_cell = next(p for p in after1["staff"] if p["id"] == filler)["cells"][day]
            assert filler_cell["workable"], filler_cell
            page.screenshot(path=str(OUT / "03-before-filler-absent-filled.png"), full_page=True)
            log.append({"step": "first_filled", "filler": names[filler],
                        "filler_cell": filler_cell["text"], "metrics": after1["metrics"]})

            # 3) 代わりに入った人が同じ日に欠勤
            cell_button(page, filler, day).click()
            page.wait_for_selector("article.vacancy >> nth=1")
            after2 = state()
            v2 = next(v for v in after2["vacancies"] if v["id"] != v1["id"])
            assert v2["status"] == "open"
            assert v2["solver_code"] in OK_CODES, f"固着: {v2['solver_code']}"
            assert v2["offers"], "代替者の再欠勤で候補0（固着）"
            replacement = v2["offers"][0]["staff_id"]
            assert replacement not in (first_absent, filler)
            page.locator(f"#vacancy-{v2['id']}").scroll_into_view_if_needed()
            page.screenshot(path=str(OUT / "04-after-filler-absent-full.png"), full_page=True)
            page.locator(f"#vacancy-{v2['id']}").screenshot(path=str(OUT / "05-second-vacancy-candidates.png"))
            log.append({"step": "filler_absent", "vacancy": v2["id"], "solver": v2["solver_code"],
                        "offers": [o["name"] for o in v2["offers"]],
                        "ineligible": len(v2["ineligible"]), "metrics": after2["metrics"]})

            # 4) 新しい欠員へ打診→承諾
            offer_card(page, v2["id"], names[replacement]).get_by_role("button", name="打診する（送信しません）").click()
            offer_card(page, v2["id"], names[replacement]).get_by_role("button", name="入れる（再検証して確定）").click()
            page.wait_for_selector(f"#vacancy-{v2['id']} >> text=充足（内部）")
            final = state()
            assert all(v["status"] == "filled" and v["fill_route"] == "internal" for v in final["vacancies"])
            assert final["metrics"]["unfilled"] == 0
            cov = next(c for c in final["coverage"] if c["day"] == day)
            assert all(s["short"] == 0 for s in cov["shifts"]), cov
            cells = {p["id"]: p["cells"][day] for p in final["staff"]}
            assert cells[replacement]["shift"] == "D"
            assert cells[filler]["kind"] == "absent" and cells[first_absent]["kind"] == "absent"
            page.screenshot(path=str(OUT / "06-final-both-filled.png"), full_page=True)
            log.append({"step": "both_filled", "replacement": names[replacement],
                        "vacancies": [(v["id"], v["status"], v["fill_route"], v["commit_code"]) for v in final["vacancies"]],
                        "coverage_day": cov, "metrics": final["metrics"]})

            # 5) 以後の別日の欠員も固着しない（受付が再開している）
            later = next(p["id"] for p in final["staff"]
                         if p["cells"][6]["shift"] == "D" and p["cells"][6]["workable"])
            cell_button(page, later, 6).click()
            page.wait_for_selector("article.vacancy >> nth=2")
            v3 = state()["vacancies"][2]
            assert v3["solver_code"] in OK_CODES and v3["offers"], v3
            page.locator(f"#vacancy-{v3['id']}").screenshot(path=str(OUT / "07-later-day-not-locked.png"))
            log.append({"step": "later_day", "vacancy": v3["id"], "solver": v3["solver_code"],
                        "offers": len(v3["offers"])})
            browser.close()
        (OUT / "filler-absent-flow.log").write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(log, ensure_ascii=False, indent=2))
        print("FILLER_ABSENT_DEMO: OK")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
