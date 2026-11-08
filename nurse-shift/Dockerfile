FROM python:3.12-slim-bookworm

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY nurse_shift ./nurse_shift
COPY README.md LICENSE NOTICE ./

ENV PORT=8080
ENV NURSE_SHIFT_DB=/tmp/nurse-shift.sqlite
EXPOSE 8080

CMD ["sh", "-c", "exec python -m uvicorn nurse_shift.main:create_app --factory --host 0.0.0.0 --port ${PORT}"]
