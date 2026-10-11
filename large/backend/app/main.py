from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from .config import BACKEND_DIR, settings
from .db import SessionLocal, init_db
from .errors import AppError
from .models import Admin
from .security import RedactSecretsFilter, hash_password
from .routers import admin, auth, manager, participant

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def ensure_admin() -> None:
    if not settings.admin_email or not settings.admin_password:
        return
    with SessionLocal() as db:
        if db.scalar(select(Admin).where(Admin.email == settings.admin_email.lower())) is None:
            db.add(Admin(email=settings.admin_email.lower(), password_hash=hash_password(settings.admin_password)))
            db.commit()


def create_app() -> FastAPI:
    for name in ("uvicorn.access", "uvicorn.error", "app"):
        logging.getLogger(name).addFilter(RedactSecretsFilter())

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        init_db()
        ensure_admin()
        yield

    app = FastAPI(title="총무 Agent API", version="0.1.0", lifespan=lifespan, docs_url="/api/docs" if settings.is_demo else None, openapi_url="/api/openapi.json" if settings.is_demo else None)

    @app.exception_handler(AppError)
    async def _app_error(_: Request, e: AppError):
        return JSONResponse(status_code=e.status, content={"error": {"code": e.code, "message": e.message, "detail": e.detail}})

    @app.exception_handler(OperationalError)
    async def _db_busy(_: Request, e: OperationalError):
        if "locked" in str(e.orig):
            return JSONResponse(status_code=503, content={"error": {"code": "CONCURRENT_UPDATE", "message": "동시에 다른 변경이 처리되어 저장하지 못했습니다. 다시 시도하세요"}})
        raise e

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, e: RequestValidationError):
        detail = [{"loc": list(err["loc"]), "msg": err["msg"]} for err in e.errors()]  # 입력값(토큰 등)은 되돌려 주지 않음
        return JSONResponse(status_code=422, content={"error": {"code": "VALIDATION_ERROR", "message": "입력값을 확인하세요", "detail": detail}})

    @app.middleware("http")
    async def csrf_header(request: Request, call_next):
        # 쿠키 세션 + SameSite=Lax 에 더해, 상태 변경 요청은 앱이 붙이는 헤더가 있어야 받는다.
        if request.method in UNSAFE_METHODS and request.url.path.startswith("/api/") and request.headers.get("x-requested-with") != "chongmu":
            return JSONResponse(status_code=403, content={"error": {"code": "CSRF_HEADER_MISSING", "message": "허용되지 않은 요청입니다"}})
        response = await call_next(request)
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.get("/api/meta")
    def meta():
        return {"app_mode": settings.app_mode, "email_mode": settings.email_mode, "llm_mode": settings.llm_mode, "demo_tools": settings.is_demo}

    app.include_router(auth.router)
    app.include_router(participant.router)
    app.include_router(manager.router)
    app.include_router(admin.router)
    if settings.is_demo:
        from .routers import dev

        app.include_router(dev.router)

    # 프런트 빌드(frontend/dist)가 있으면 같은 서버에서 SPA 로 제공
    dist = BACKEND_DIR.parent / "frontend" / "dist"
    if dist.exists():
        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith("api/"):
                return JSONResponse(status_code=404, content={"error": {"code": "NOT_FOUND", "message": "없는 API"}})
            f = (dist / path).resolve()
            if path and f.is_file() and dist.resolve() in f.parents:
                return FileResponse(f)
            return FileResponse(dist / "index.html")

    return app


app = create_app()
