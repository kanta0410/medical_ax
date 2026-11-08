# 初回ソース保全記録

この記録は `kanta0410/medical_ax` への初回アプリ保全だけを残す。テスト合格、デプロイ、公開は含まない。

## 対象

- リポジトリ: https://github.com/kanta0410/medical_ax
- owner: `kanta0410`（User）
- 名前: `medical_ax`
- 可視性: private（`gh repo view` の `isPrivate=true`、`visibility=PRIVATE`。API の `private=true`）
- 既定ブランチ: `main`
- 保全前 `main`: `69ad65ef30900fcb1819a8674895073d8815532f`
- 保全前 tree: `README.md` のみ（`# medical_ax`）
- root `README.md` sha256: `bab35f6659f26c18d950a9108773ad76f49075f698ecd189daa46e2ff93ba2d8`
- root `README.md` は上書きしていない。このブランチでも blob は同じ。

## 配置

添付 manifest の 28 ファイルを、内容を変えず `nurse-shift/` 配下へ元の相対パスのまま置いた。`nurse-shift/` は保全前の `main` に存在しなかった。

manifest 自体は `docs/verification/source-manifest.sha256`。sha256 は `7cfc1199a2ebf65eb1d0e2dc8bd9e0332a9a13d2dccfeabdbcbca0235f86dbe1`、28 行。tar 内の `source-manifest.sha256` とバイト一致。

展開元 tar の sha256 は `25926b40a85deee946dc86b20e29a05f89862f357ea8c0893992174ea75f6e40`。パス逸脱、シンボリックリンク、絶対パスはなかった。展開後、`nurse-shift/` で `sha256sum -c ../docs/verification/source-manifest.sha256` は 28/28 OK。

アプリ README（`nurse-shift/README.md`）は、失効した別リポジトリ名とローカルパスを本文に残したまま保全した。書き換えない。

## スキャン

値は記録しない。検査対象は tar 展開先の全 30 ファイル（追跡する 28 ファイルと、下の除外 2 ファイル）。外部の秘密スキャンサービスへは送っていない。

- gitleaks 8.30.1（`detect --no-git --redact`、ローカル実行）: findings 0。配布物 sha256 `551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb` を照合してから実行。
- 静的パターン（秘密鍵、クラウド/API トークン、接続文字列、秘密の代入、メールアドレス、電話番号、郵便番号、マイナンバー様の 12 桁、高エントロピー引用符文字列）: 0 件。
- ファイル名: `.env`、秘密鍵、DB、ログ、zip、tar は展開結果に無い。
- 画面とデータは架空ラベル。施設名は「青葉ケ丘病院 西3病棟（架空の看護病棟）」、職員名は「青葉」で揃えたデモ名、応募名は「桐谷 透」と「架空 資格不足」。画面に「架空データ・外部送信なし」。実在人物データベースとの照合はしていない。

疑わしい秘密・資格情報・実個人情報・実データは、この静的検査の範囲では見つからなかった。

## 限界

- 正規表現と gitleaks の静的検査。実行時メモリ、難読化、別エンコーディングの秘密は見ていない。
- 人名が実在者と一致しないことは保証しない。連絡先・識別番号のパターンは無かった。
- この tar に `.venv`、キャッシュ、SQLite、旧 ZIP、デモ動画は含まれておらず、それらの中身は検査していない。
- pytest と UI デモは再実行していない。この記録はソース保存の照合であり、21 tests の合格証明ではない。

## 追跡しないもの

manifest 28 件に、除外必須のファイル（`.env`、秘密鍵、DB、ログ、venv、キャッシュ、zip、tar）は無かった。

tar にあって commit しないもの:

| パス | 理由 | sha256 |
|---|---|---|
| `sha256sum-check.txt` | 旧 `sha256sum -c` 実行ログ。本体や今回の検査記録と混ぜない | `6b6cf18d671a46396dbb46de54b4dcf0711f27ec9baa92d59d0e3680e22b5884` |
| 添付 tar 自体 | スナップショット容器。ソースの二重保存をしない | `25926b40a85deee946dc86b20e29a05f89862f357ea8c0893992174ea75f6e40` |

root `.gitignore` は今後の秘密、venv、キャッシュ、DB、ログ、アーカイブ、デモ動画を追跡しないための新規ファイル。`nurse-shift/.gitignore` は 28 ファイルの一つとして元の内容のまま。

## この commit でしないこと

`main` は動かさない。merge、force push、履歴改写、削除、公開化、招待、権限変更、CI、Issue、branch protection、Cloud Run、課金、提出はしない。
