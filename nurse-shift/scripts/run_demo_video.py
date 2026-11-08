"""ハッピーパスの通しデモを録画する（音声なし・架空データのみ・外部送信なし）。

勤務表 → 欠員 → 内部候補 → 打診 → 承諾（再検証して確定）→ 再起動・再読込で状態維持
→ 別の欠員で全員「無理」→ 管理者確認 → 資格不足の架空応募が拒否 → 条件を満たす架空応募で充足。

既存の scripts/run_demo.py は変更しない。録画用の字幕は、このスクリプトが画面右下に重ねる
表示だけで、アプリ本体には入らない。出力は NURSE_SHIFT_DEMO_OUT（webm、ffmpeg があれば mp4 も）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = Path(os.environ.get("NURSE_SHIFT_DEMO_DB", "/tmp/nurse-shift-video-demo.sqlite"))
PORT = int(os.environ.get("NURSE_SHIFT_DEMO_PORT", "8893"))
BASE = f"http://127.0.0.1:{PORT}"
OUT = Path(os.environ.get("NURSE_SHIFT_DEMO_OUT", str(ROOT / "demo-artifacts" / "video")))
CHROME = os.environ.get("NURSE_SHIFT_CHROME", "/usr/bin/google-chrome")
PAUSE_MS = int(os.environ.get("NURSE_SHIFT_VIDEO_PAUSE_MS", "3000"))
SIZE = {"width": 1280, "height": 800}

CAPTION_JS = """
(text) => {
  let el = document.getElementById('demo-video-caption');
  if (!el) {
    el = document.createElement('div');
    el.id = 'demo-video-caption';
    el.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:99999;max-width:560px;' +
      'background:rgba(20,20,20,.85);color:#fff;font:600 16px/1.5 sans-serif;padding:10px 14px;' +
      'border-radius:8px;pointer-events:none;white-space:pre-line';
    document.body.appendChild(el);
  }
  el.textContent = '【録画字幕】架空データ・外部送信なし\\n' + text;
}
"""


def wait_health() -> None:
    for _ in range(75):
        try:
            with urllib.request.urlopen(BASE + "/api/health", timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("server did not start")


def launch() -> subprocess.Popen:
    env = os.environ.copy()
    env["NURSE_SHIFT_DB"] = str(DB)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "nurse_shift.main:create_app", "--factory",
         "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    wait_health()
    return proc


def stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def state() -> dict:
    with urllib.request.urlopen(BASE + "/api/state", timeout=10) as response:
        return json.loads(response.read().decode())


def main() -> None:
    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    if DB.exists():
        DB.unlink()
    log: list[dict] = []
    started = time.monotonic()
    proc = launch()
    video_path = None
    try:
        with sync_playwright() as playwright:
            kwargs = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
            if Path(CHROME).exists():
                kwargs["executable_path"] = CHROME
            browser = playwright.chromium.launch(**kwargs)
            context = browser.new_context(
                viewport=SIZE, record_video_dir=str(OUT), record_video_size=SIZE
            )
            page = context.new_page()

            def caption(text: str, top: bool = False) -> None:
                if top:
                    page.evaluate("window.scrollTo(0, 0)")
                page.evaluate(CAPTION_JS, text)
                page.wait_for_timeout(PAUSE_MS)

            def focus(selector: str) -> None:
                page.locator(selector).first.scroll_into_view_if_needed()

            page.goto(BASE, wait_until="networkidle")
            assert page.get_by_text("架空データ・外部送信なし").first.is_visible()
            caption("1/8 CP-SAT で計算した架空病棟の勤務表", top=True)
            log.append({"step": "board", "metrics": state()["metrics"]})

            page.get_by_role("button", name="この勤務を欠勤にする").first.click()
            page.wait_for_selector("article.vacancy")
            focus("article.vacancy")
            opened = state()["vacancies"][0]
            caption(f"2/8 欠勤 → 欠員#{opened['id']}（{opened['solver_code']}）と内部候補を再計算")
            log.append({"step": "shortage", "vacancy": opened["id"], "solver": opened["solver_code"],
                        "offers": len(opened["offers"])})

            page.get_by_role("button", name="打診する（送信しません）").first.click()
            focus("article.vacancy")
            caption("3/8 候補へ打診（画面内のみ・送信しません）")

            page.get_by_role("button", name="入れる（再検証して確定）").click()
            page.wait_for_selector("text=充足（内部）")
            focus("article.vacancy")
            internal = state()["vacancies"][0]
            caption(f"4/8 承諾 → 解き直して絶対条件を確認し確定（{internal['commit_code']}）")
            log.append({"step": "internal_filled", "route": internal["fill_route"],
                        "commit": internal["commit_code"]})

            stop(proc)
            proc = launch()
            page.reload(wait_until="networkidle")
            reloaded = state()["vacancies"][0]
            assert reloaded["id"] == internal["id"] and reloaded["status"] == "filled"
            focus("article.vacancy")
            caption("5/8 サーバー再起動・再読込後も充足が残る（SQLite 永続）")
            log.append({"step": "reloaded", "vacancy": reloaded["id"], "status": reloaded["status"]})

            page.get_by_role("button", name="架空の勤務表を計算し直す").click()
            page.wait_for_selector("text=この勤務を欠勤にする")
            page.get_by_role("button", name="この勤務を欠勤にする").first.click()
            page.wait_for_selector("article.vacancy")
            while page.get_by_role("button", name="打診する（送信しません）").count():
                page.get_by_role("button", name="打診する（送信しません）").first.click()
                page.get_by_role("button", name="無理").first.click()
            page.get_by_role("button", name="管理者として、この欠員の募集シミュレーションを開く").click()
            focus("article.vacancy")
            caption("6/8 別の欠員で内部候補が全員「無理」→ 管理者確認で募集シミュレーション")

            unqualified = None
            if page.get_by_role("button", name="資格不足の応募を試す").count():
                page.get_by_role("button", name="資格不足の応募を試す").click()
                page.wait_for_selector(".alert")
                unqualified = page.locator(".alert").inner_text()
                page.locator(".alert").first.scroll_into_view_if_needed()
                caption("7/8 資格不足の架空応募は検証で拒否")
            log.append({"step": "unqualified", "alert": unqualified})

            page.get_by_role("button", name="架空の応募を検証する").click()
            page.wait_for_selector("text=充足（架空応募")
            focus("article.vacancy")
            external = state()["vacancies"][0]
            caption("8/8 条件を満たす架空応募で充足し、募集を閉じる")
            log.append({"step": "external_filled", "route": external["fill_route"],
                        "posting": external["posting"]["status"], "metrics": state()["metrics"]})
            page.wait_for_timeout(PAUSE_MS)

            video_path = Path(page.video.path())
            context.close()
            browser.close()
    finally:
        stop(proc)

    final = OUT / "nurse-shift-happy-path.webm"
    if video_path and video_path.exists():
        video_path.replace(final)
    result = {"webm": str(final), "elapsed_s": round(time.monotonic() - started, 1), "steps": log}
    if shutil.which("ffmpeg") and final.exists():
        mp4 = OUT / "nurse-shift-happy-path.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(final), "-an", "-c:v", "libx264",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(mp4)],
            check=True,
        )
        result["mp4"] = str(mp4)
    (OUT / "video-flow.log").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
