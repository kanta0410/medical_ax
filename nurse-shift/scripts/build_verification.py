"""pytest の生ログ、ソースマニフェスト、1280x800 の画面を artifacts に書く。"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
OUT = Path("/opt/cursor/artifacts/verification")
DB = Path("/tmp/nurse-shift-verify.sqlite")
PORT = 8766
BASE = f"http://127.0.0.1:{PORT}"
SKIP_DIRS = {".pytest_cache", "__pycache__", "data", ".venv"}
SKIP_SUFFIXES = {".pyc", ".sqlite", ".sqlite-journal"}


def run(cmd: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True, check=False)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def source_files() -> list[Path]:
    found = []
    for path in sorted(APP.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(APP).parts):
            continue
        if path.suffix in SKIP_SUFFIXES:
            continue
        if path.name.endswith(".sqlite-journal"):
            continue
        found.append(path)
    return found


def write_manifest_and_source() -> None:
    source_root = OUT / "source"
    if source_root.exists():
        shutil.rmtree(source_root)
    lines = []
    bundle = []
    for path in source_files():
        relative = path.relative_to(APP).as_posix()
        digest = sha256(path)
        lines.append(f"{digest}  {relative}")
        target = source_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        text = path.read_text(encoding="utf-8", errors="replace")
        bundle.append(f"\n===== {relative}  sha256={digest} =====\n{text}")
    (OUT / "source-manifest.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT / "source-all.txt").write_text("".join(bundle), encoding="utf-8")


def write_environment() -> None:
    freeze = run([sys.executable, "-m", "pip", "freeze"], APP)
    (OUT / "pip-freeze.txt").write_text(freeze.stdout, encoding="utf-8")
    os_release = Path("/etc/os-release").read_text(encoding="utf-8")
    text = "\n".join(
        [
            f"python={sys.version}",
            f"executable={sys.executable}",
            f"platform={platform.platform()}",
            f"uname={os.uname().sysname} {os.uname().release} {os.uname().machine}",
            "",
            os_release,
        ]
    )
    (OUT / "environment.txt").write_text(text, encoding="utf-8")


def write_cases_md() -> None:
    rows = []
    raw = (OUT / "cases.jsonl").read_text(encoding="utf-8")
    for line in raw.splitlines():
        if line.strip():
            rows.append(json.loads(line))
    parts = [
        "# 検証ケース",
        "",
        "各ケースの actual は pytest 実行中に記録した実測です。合否の生ログは pytest-v.log です。",
        "休息不足の時間文言は候補カードには出ません。判定は CP-SAT の INFEASIBLE と coverage_errors です。",
        "",
    ]
    for row in rows:
        parts.append(f"## {row['case_id']}")
        parts.append("")
        parts.append("### 入力")
        parts.append("```json")
        parts.append(json.dumps(row["inputs"], ensure_ascii=False, indent=2))
        parts.append("```")
        parts.append("")
        parts.append("### 操作")
        for step in row["operations"]:
            parts.append(f"- {step}")
        parts.append("")
        parts.append("### 期待")
        parts.append("```json")
        parts.append(json.dumps(row["expected"], ensure_ascii=False, indent=2))
        parts.append("```")
        parts.append("")
        parts.append("### 実測")
        parts.append("```json")
        parts.append(json.dumps(row["actual"], ensure_ascii=False, indent=2))
        parts.append("```")
        parts.append("")
        parts.append(f"証拠: verification/cases.jsonl の case_id={row['case_id']}、verification/pytest-v.log")
        parts.append("")
    (OUT / "cases.md").write_text("\n".join(parts), encoding="utf-8")


def wait_health() -> None:
    for _ in range(50):
        try:
            with urllib.request.urlopen(BASE + "/api/health", timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("server did not start")


def read_state() -> dict:
    with urllib.request.urlopen(BASE + "/api/state", timeout=30) as response:
        return json.loads(response.read().decode())


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
        cwd=APP,
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
        cwd=APP,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    wait_health()
    return proc


def shoot(page, name: str, note: str, log: list[dict]) -> None:
    path = OUT / "screenshots" / name
    page.screenshot(path=str(path), full_page=False)
    state_path = path.with_suffix(".state.json")
    current = read_state()
    state_path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    box = page.viewport_size
    log.append(
        {
            "image": f"verification/screenshots/{name}",
            "state": f"verification/screenshots/{path.with_suffix('.state.json').name}",
            "viewport": box,
            "full_page": False,
            "operation": note,
            "metrics": current["metrics"],
            "roster_code": current["roster"]["code"],
            "vacancies": [
                {
                    "id": item["id"],
                    "status": item["status"],
                    "shift": item["shift"],
                    "staff_id": item["staff_id"],
                    "day": item["day"],
                    "have": item["have"],
                    "need": item["need"],
                    "short": item["short"],
                    "fill_route": item["fill_route"],
                    "filled_staff_id": item["filled_staff_id"],
                    "filled_external_name": item["filled_external_name"],
                    "solver_code": item["solver_code"],
                    "offer_ids": [offer["staff_id"] for offer in item["offers"]],
                }
                for item in current["vacancies"]
            ],
        }
    )


def capture_screens() -> None:
    from playwright.sync_api import sync_playwright

    shots = OUT / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    log: list[dict] = []
    proc = start_server()
    try:
        wait_health()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path="/usr/local/bin/google-chrome",
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(BASE, wait_until="networkidle")
            shoot(page, "01-initial-1280x800.png", "GET / 。CP-SAT で作った初期表。スクロールなし。", log)

            page.get_by_role("button", name="この勤務を欠勤にする").first.click()
            page.wait_for_selector("#vacancy-1")
            page.locator("#vacancy-1").scroll_into_view_if_needed()
            shoot(page, "02-vacancy-1280x800.png", "最初の「この勤務を欠勤にする」を押した直後。欠員カードを表示範囲へ寄せた。", log)

            page.get_by_role("button", name="打診する（送信しません）").first.click()
            page.get_by_role("button", name="入れる（再検証して確定）").click()
            page.wait_for_selector("text=充足（内部）")
            page.locator("#vacancy-1").scroll_into_view_if_needed()
            shoot(page, "03-internal-filled-1280x800.png", "先頭候補を打診し、「入れる（再検証して確定）」を押した。", log)

            proc = restart(proc)
            page.goto(BASE + "#vacancy-1", wait_until="networkidle")
            page.locator("#vacancy-1").scroll_into_view_if_needed()
            shoot(
                page,
                "03b-internal-after-restart-1280x800.png",
                "サーバプロセスを落として同じ sqlite で再起動し、確定済みの欠員カードを再表示した。",
                log,
            )

            page.get_by_role("button", name="架空の勤務表を計算し直す").click()
            page.wait_for_selector("text=この勤務を欠勤にする")
            page.get_by_role("button", name="この勤務を欠勤にする").first.click()
            page.wait_for_selector("#vacancy-1")
            while page.get_by_role("button", name="打診する（送信しません）").count():
                page.get_by_role("button", name="打診する（送信しません）").first.click()
                page.get_by_role("button", name="無理").first.click()
            page.get_by_role("button", name="管理者として、この欠員の募集シミュレーションを開く").click()
            page.locator("#vacancy-1").scroll_into_view_if_needed()
            shoot(
                page,
                "04a-admin-gate-1280x800.png",
                "全員の候補に打診して「無理」としたあと、管理者の募集シミュレーションを開いた。まだ応募していない。",
                log,
            )
            if page.get_by_role("button", name="資格不足の応募を試す").count():
                page.get_by_role("button", name="資格不足の応募を試す").click()
                page.wait_for_selector(".alert")
                page.locator("#vacancy-1").scroll_into_view_if_needed()
                shoot(
                    page,
                    "04b-unqualified-1280x800.png",
                    "「資格不足の応募を試す」を押した。募集は開いたまま。",
                    log,
                )
            page.get_by_role("button", name="架空の応募を検証する").click()
            page.wait_for_selector("text=充足（架空応募")
            page.locator("h3", has_text="募集シミュレーション").scroll_into_view_if_needed()
            shoot(page, "04-external-filled-1280x800.png", "「架空の応募を検証する」（桐谷 透）を押し、募集シミュレーションの見出しまでスクロールして閉じた状態を撮った。", log)
            browser.close()
        (shots / "operations.json").write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    cases = OUT / "cases.jsonl"
    if cases.exists():
        cases.unlink()
    env = os.environ.copy()
    env["VERIFICATION_CASES"] = str(cases)
    pytest = run([sys.executable, "-m", "pytest", "-v", "--tb=short"], APP, env)
    log = pytest.stdout + ("\n" + pytest.stderr if pytest.stderr else "")
    (OUT / "pytest-v.log").write_text(log, encoding="utf-8")
    if pytest.returncode != 0:
        print(log)
        raise SystemExit(pytest.returncode)
    write_cases_md()
    write_environment()
    write_manifest_and_source()
    capture_screens()
    commands = "\n".join(
        [
            "cd .  # project root",
            "VERIFICATION_CASES=/opt/cursor/artifacts/verification/cases.jsonl \\",
            "python3 -m pytest -v --tb=short",
            "python3 scripts/build_verification.py",
            "python3 -m uvicorn nurse_shift.main:create_app --factory --host 127.0.0.1 --port 8080",
        ]
    )
    (OUT / "commands.txt").write_text(commands + "\n", encoding="utf-8")
    gaps = """# テスト合格と、画面にまだ無いもの

合格しているもの:
- 内部候補の打診と再検証つき確定
- 全員辞退後の管理者ゲートと架空応募による充足
- 夜勤不可の応募を拒否し、欠員を開いたまま残す
- 準夜の翌1:00から翌日日勤8:30まで7.5時間を、検証関数とCP-SATが拒否する
- 充足済みの欠員へ2人目を確定しない（順次と同時）
- 別プロセスで同じsqliteを開いても確定結果が残る

画面側の不足:
- 休息不足の「7.5時間」は、不採用候補の理由文には出ていない。不採用文は INFEASIBLE の定型文。時間は検証関数の戻りと、採用案の最短間隔に出る。
- 同時承諾の409は2スレッドのAPI検証で、ブラウザの二重クリック映像は撮っていない。
- 公開デプロイ、外部求人への送信、法令適合の判定はしていない。
"""
    (OUT / "ui-gaps.md").write_text(gaps, encoding="utf-8")
    print("verification artifacts written")


if __name__ == "__main__":
    main()
