# 西3病棟の欠員復旧（ローカル作業ツリー）

架空の看護病棟で、欠員を内部候補か架空応募で埋めるデモです。画面の「架空データ・外部送信なし」が示すとおり、実在の個人・医療データはなく、打診と求人は画面の外へ出ません。

このディレクトリは保全されたローカル作業物です（例: `/workspace/share/gdgoc/final-source-20261107-2049/`）。専用 private リポジトリ `kanta0410/gdgoc-nurse-shift` は、この環境の GitHub 権限では作成できませんでした。Vault（`ben1san/benician_hiroyuki`）には置かないでください。デプロイ、課金、公開、提出はしません。

## 起動

このディレクトリ（`nurse_shift/` と同階層）で実行します。`cd app` は不要です。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn nurse_shift.main:create_app --factory --host 127.0.0.1 --port 8080
```

ブラウザで http://127.0.0.1:8080/ を開きます。既定のデータベースは `data/demo.sqlite`（環境変数 `NURSE_SHIFT_DB` で上書き可）です。消すと、次の起動で CP-SAT が勤務表を作り直します。

```bash
python -m pytest
```

画面デモ（Playwright）は `scripts/run_demo.py` です。出力先は `NURSE_SHIFT_DEMO_OUT`、Chrome は `NURSE_SHIFT_CHROME` で指定できます。
境界ケースの画面証拠（同時承諾で二人目が拒否される・内部候補ゼロで解なし・日跨ぎ準夜の休息理由）は `scripts/run_edge_demo.py` です。出力は `$NURSE_SHIFT_DEMO_OUT/edge/` に保存されます。
代わりに入った人が同じ日にさらに欠勤しても固まらず、二つ目の欠員も打診→承諾で充足できることの画面証拠は `scripts/run_filler_absent_demo.py` です（既定 PORT 8881、`NURSE_SHIFT_DEMO_PORT` で変更可）。
通しデモの録画（勤務表→欠員→打診→承諾→再起動後も維持→資格不足の拒否→架空応募で充足、音声なし・約30秒）は `scripts/run_demo_video.py` です。`$NURSE_SHIFT_DEMO_OUT` に webm（ffmpeg があれば mp4 も）を保存します。右下の字幕は録画用の重ね表示で、アプリ本体には入りません。

## 流れ

1. 初期勤務表は OR-Tools CP-SAT で計算します。サンプルの読み込みではありません。
2. 勤務セルから欠勤にすると、同じ欠員 ID で不足人数と候補が出ます。
3. 候補は資格、重複、休息を満たす再計算です。打診と「入れる / 無理」は送信しません。
4. 「入れる」は確定前に解き直し、絶対条件を崩す案は書き込みません。
5. 候補が全員「無理」、または解なしのときは、管理者の確認のあと募集シミュレーションを開きます。
6. 架空の応募を検証し、通ればその欠員を充足して募集を閉じます。

変更セル数、未充足数、希望違反数は、保存された勤務と欠員から数えています。求解状態が FEASIBLE のときは最適性未証明、UNKNOWN は解なしと別です。デモの休息は 11 時間で、法令適合の判定ではありません。

Cloud Run 用の Dockerfile は同梱しています。イメージの作成とデプロイはしていません。

Dockerfile と同じ起動方法（`PORT` と `NURSE_SHIFT_DB` を環境変数で渡し、`exec` で uvicorn を PID 1 にする）を本物の HTTP サーバーで確かめるスクリプトがあります。外部送信・イメージ作成・課金はしません。

```
SMOKE_PORT=8843 python scripts/run_container_smoke.py
```

health と画面が 200、二人が同時に HTTP で「入れる」を送っても 200 と 409 になり充足は 1 件、同じ DB で再起動しても充足が残ることを確かめます。

Cloud Run に載せる場合の注意（未実施）: 状態はコンテナ内の SQLite（既定 `/tmp/nurse-shift.sqlite`）に保存するため、インスタンスが複数になると状態が分かれ、二重充足防止がインスタンスをまたいで効きません。デモでは最大インスタンス数を 1 にし、インスタンスが入れ替わるとデモ状態は初期化されることを前提にしてください。永続化と複数インスタンス対応は今回の範囲外です。
