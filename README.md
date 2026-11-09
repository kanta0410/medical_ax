# medical_ax

看護の勤務表で、急な欠勤を埋めるデモアプリ(DevFest GDGoC Japan ハッカソン 2026)。
データはすべて架空で、打診や求人は画面の外へ送りません。

- アプリ本体: [`nurse-shift/`](nurse-shift/)(FastAPI + Google OR-Tools CP-SAT)
- 起動・流れ・Cloud Run での注意: [`nurse-shift/README.md`](nurse-shift/README.md)

## すぐ動かす

```bash
cd nurse-shift
uv run --python 3.12 --with-requirements requirements.txt \
  python -m uvicorn nurse_shift.main:create_app --factory --port 8080
```

http://127.0.0.1:8080/ を開き、勤務のマスを押すと欠員ができます。
