"""ローカル画面。外部へは送らない。"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from nurse_shift.domain import Problem
from nurse_shift.service import Service, ServiceError

BAD_INPUT = (KeyError, TypeError, ValueError)

ROOT = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(ROOT / "templates"))


def create_app(db_path: str | None = None, problem: Problem | None = None) -> FastAPI:
    path = db_path or os.environ.get("NURSE_SHIFT_DB") or str(ROOT.parent / "data" / "demo.sqlite")
    service = Service(path, problem)
    service.init()
    app = FastAPI(title="西3病棟 欠員復旧")
    app.state.service = service
    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True}

    @app.get("/api/state")
    def state() -> dict:
        return service.snapshot()

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, error: str = ""):
        return TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {"snap": service.snapshot(), "error": error},
        )

    @app.post("/api/reset")
    async def reset(request: Request):
        _body, as_json = await _read(request)
        service.reset()
        return _done(as_json, service)

    @app.post("/api/vacancies")
    async def open_vacancy(request: Request):
        body, as_json = await _read(request)
        try:
            staff_id, day = str(body["staff_id"]), int(body["day"])
        except BAD_INPUT:
            return _fail(as_json, ServiceError("スタッフと対象日を指定してください"))
        try:
            vacancy_id = service.open_vacancy(staff_id, day)
        except ServiceError as exc:
            return _fail(as_json, exc)
        if as_json:
            payload = service.snapshot()
            payload["vacancy_id"] = vacancy_id
            return JSONResponse(payload)
        return RedirectResponse(f"/#vacancy-{vacancy_id}", status_code=303)

    @app.post("/api/vacancies/{vacancy_id}/ask")
    async def ask(vacancy_id: int, request: Request):
        body, as_json = await _read(request)
        if not body.get("staff_id"):
            return _fail(as_json, ServiceError("打診する候補を指定してください"), vacancy_id)
        try:
            service.ask(vacancy_id, str(body["staff_id"]))
        except ServiceError as exc:
            return _fail(as_json, exc, vacancy_id)
        return _done(as_json, service, vacancy_id)

    @app.post("/api/vacancies/{vacancy_id}/respond")
    async def respond(vacancy_id: int, request: Request):
        body, as_json = await _read(request)
        if not body.get("staff_id") or not body.get("decision"):
            return _fail(as_json, ServiceError("応答する候補と応答内容を指定してください"), vacancy_id)
        try:
            service.respond(vacancy_id, str(body["staff_id"]), str(body["decision"]))
        except ServiceError as exc:
            return _fail(as_json, exc, vacancy_id)
        return _done(as_json, service, vacancy_id)

    @app.post("/api/vacancies/{vacancy_id}/admin")
    async def admin(vacancy_id: int, request: Request):
        _body, as_json = await _read(request)
        try:
            service.confirm_admin(vacancy_id)
        except ServiceError as exc:
            return _fail(as_json, exc, vacancy_id)
        return _done(as_json, service, vacancy_id)

    @app.post("/api/vacancies/{vacancy_id}/apply")
    async def apply(vacancy_id: int, request: Request):
        body, as_json = await _read(request)
        try:
            result = service.mock_apply(
                vacancy_id,
                str(body.get("name") or "桐谷 透"),
                _flag(body.get("night_ok", "1")),
                _flag(body.get("leader", "1")),
            )
        except ServiceError as exc:
            return _fail(as_json, exc, vacancy_id)
        if as_json:
            payload = service.snapshot()
            payload["apply"] = result
            return JSONResponse(payload)
        anchor = f"/#vacancy-{vacancy_id}"
        if not result["accepted"]:
            anchor = f"/?error={quote(result['reason'])}#vacancy-{vacancy_id}"
        return RedirectResponse(anchor, status_code=303)

    return app


async def _read(request: Request) -> tuple[dict, bool]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            data = await request.json()
        except ValueError:
            data = None
        return (data if isinstance(data, dict) else {}), True
    form = await request.form()
    return {key: form.get(key) for key in form.keys()}, False


def _flag(value) -> bool:
    return str(value).lower() in {"1", "true", "yes", "on"}


def _done(as_json: bool, service: Service, vacancy_id: int | None = None):
    if as_json:
        return JSONResponse(service.snapshot())
    anchor = "/" if vacancy_id is None else f"/#vacancy-{vacancy_id}"
    return RedirectResponse(anchor, status_code=303)


def _fail(as_json: bool, exc: ServiceError, vacancy_id: int | None = None):
    if as_json:
        return JSONResponse({"error": exc.message}, status_code=exc.status)
    target = f"/?error={quote(exc.message)}"
    if vacancy_id is not None:
        target += f"#vacancy-{vacancy_id}"
    return RedirectResponse(target, status_code=303)
