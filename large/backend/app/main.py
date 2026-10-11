"""LARGE(대모임) 앱 구성.

- configure_large(app): LARGE 의 API(/api/large/...)·오류 처리·보안 미들웨어를 주어진 FastAPI 앱에 붙인다.
  공용 서버(hub/main.py)가 이 함수를 불러 SMALL 과 한 서버로 묶는다.
- add_large_ui(app): 빌드된 React 화면을 /large/ 에서 제공.
- create_app(): LARGE 만 단독으로 띄울 때(개발·테스트).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from .config import API_PREFIX, BACKEND_DIR, UI_PREFIX, settings
from .db import SessionLocal, init_db
from .errors import AppError
from .models import Admin
from .routers import admin, auth, manager, participant
from .security import RedactSecretsFilter, hash_password
from .services.scheduler import scheduler

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
DIST = BACKEND_DIR.parent / "frontend" / "dist"


def ensure_admin() -> None:
    if not settings.admin_email or not settings.admin_password:
        return
    with SessionLocal() as db:
        if db.scalar(select(Admin).where(Admin.email == settings.admin_email.lower())) is None:
            db.add(Admin(email=settings.admin_email.lower(), password_hash=hash_password(settings.admin_password)))
            db.commit()


def startup() -> None:
    """DB 테이블 준비 + 총무 계정 보장 + (설정 시) 이메일 스케줄러 시작. 단독 실행·공용 서버 모두 시작 시 한 번 부른다."""
    init_db()
    ensure_admin()
    if settings.email_scheduler:
        from .services.scheduler import scheduler

        scheduler.start()


def shutdown() -> None:
    """서버 종료 시 스케줄러 스레드 정리."""
    from .services.scheduler import scheduler

    scheduler.stop()


def is_large_api(path: str) -> bool:
    return path == API_PREFIX or path.startswith(API_PREFIX + "/")


def configure_large(app: FastAPI) -> None:
    for name in ("uvicorn.access", "uvicorn.error", "app"):
        logging.getLogger(name).addFilter(RedactSecretsFilter())

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
        # LARGE API 에만 적용한다(SMALL API 의 기존 동작은 건드리지 않음).
        # 쿠키 세션 + SameSite=Lax 에 더해, 상태 변경 요청은 앱이 붙이는 헤더가 있어야 받는다.
        large = is_large_api(request.url.path)
        if large and request.method in UNSAFE_METHODS and request.headers.get("x-requested-with") != "chongmu":
            return JSONResponse(status_code=403, content={"error": {"code": "CSRF_HEADER_MISSING", "message": "허용되지 않은 요청입니다"}})
        response = await call_next(request)
        if large:
            response.headers.setdefault("Referrer-Policy", "no-referrer")
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.get(API_PREFIX + "/meta")
    def meta():
        return {
            "system": "large",
            "app_mode": settings.app_mode,
            "email_mode": settings.email_mode,
            "llm_mode": settings.llm_mode,
            "demo_tools": settings.is_demo,
            "scheduler": {"enabled": scheduler.running and not scheduler.paused, "interval_seconds": scheduler.interval},
        }

    app.include_router(auth.router)
    app.include_router(participant.router)
    app.include_router(manager.router)
    app.include_router(admin.router)
    if settings.is_demo:
        from .routers import dev

        app.include_router(dev.router)

    @app.api_route(API_PREFIX + "/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    def large_api_not_found(rest: str):
        # 없는 LARGE API 는 다른 시스템(SMALL)으로 넘기지 않고 여기서 끝낸다.
        return JSONResponse(status_code=404, content={"error": {"code": "NOT_FOUND", "message": "없는 API"}})


def add_large_ui(app: FastAPI, dist: Path = DIST) -> None:
    """빌드된 React 화면을 /large/ 아래에서 제공. 내부 경로(/large/admin 등)를 직접 열거나 새로고침해도 index.html."""

    @app.get(UI_PREFIX, include_in_schema=False)
    def large_root_redirect():
        return RedirectResponse(UI_PREFIX + "/")

    @app.get(UI_PREFIX + "/{path:path}", include_in_schema=False)
    def large_ui(path: str):
        if not (dist / "index.html").exists():
            return JSONResponse(
                status_code=503,
                content={"error": {"code": "LARGE_UI_NOT_BUILT", "message": "LARGE 화면이 아직 빌드되지 않았습니다: cd large/frontend && npm run build"}},
            )
        f = (dist / path).resolve()
        if path and f.is_file() and dist.resolve() in f.parents:
            # 해시가 붙은 자산은 오래 캐시해도 되지만 index.html 은 항상 새로 받는다.
            headers = {"Cache-Control": "public, max-age=31536000, immutable"} if path.startswith("assets/") else {}
            return FileResponse(f, headers=headers)
        if path.startswith("assets/"):
            return JSONResponse(status_code=404, content={"error": {"code": "NOT_FOUND", "message": "없는 파일"}})
        return FileResponse(dist / "index.html", headers={"Cache-Control": "no-store"})


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        startup()
        yield
        shutdown()

    app = FastAPI(
        title="총무 Agent LARGE API",
        version="0.2.0",
        lifespan=lifespan,
        docs_url=API_PREFIX + "/docs" if settings.is_demo else None,
        openapi_url=API_PREFIX + "/openapi.json" if settings.is_demo else None,
    )
    configure_large(app)
    add_large_ui(app)

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse(UI_PREFIX + "/")

    return app


app = create_app()
