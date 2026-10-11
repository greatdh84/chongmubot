"""총무봇 공용 서버.

경로
  /                 → SMALL 의 기존 시작 화면(/demo/index.html: 시작하기 → 모임 규모 선택)
  /demo/*           SMALL 화면·자산 (app/server.py 의 정적 제공 그대로)
  /small, /small/   → /demo/index.html (SMALL 화면의 기존 주소로 이동)
  /api/...          SMALL API (app/server.py 그대로: /api/slack/*, /api/config, /api/survey, /api/places/* …)
  /large/*          LARGE React 화면 (large/frontend/dist, 내부 경로 새로고침 시 index.html)
  /api/large/*      LARGE API (large/backend)
  /api/hub          두 시스템 연결 상태(공개해도 되는 값만)
실행: .venv/bin/python -m hub   (기본 http://127.0.0.1:8787)
"""
from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

ROOT = Path(__file__).resolve().parent.parent
LARGE_BACKEND = ROOT / "large" / "backend"
# LARGE 백엔드 패키지 이름도 'app' 이다. 저장소 루트의 app/(SMALL, __init__.py 없음)보다 먼저 찾도록 맨 앞에 둔다.
if str(LARGE_BACKEND) not in sys.path:
    sys.path.insert(0, str(LARGE_BACKEND))

from app.config import settings as large_settings  # noqa: E402  (LARGE)
import app.main as large_main  # noqa: E402
from app.main import DIST as LARGE_DIST, add_large_ui, configure_large, startup as large_startup  # noqa: E402

from . import small_bridge  # noqa: E402  (SMALL — import 만으로는 Slack 연결 안 함)


def create_app(large_dist: Path = LARGE_DIST, start_small_socket: bool | None = None) -> FastAPI:
    if start_small_socket is None:
        start_small_socket = os.environ.get("HUB_SMALL_SOCKET", "on") != "off"

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        large_startup()
        if start_small_socket:
            # app/server.py 를 단독 실행할 때와 같은 동작: SLACK_APP_TOKEN 이 있을 때만 Socket Mode 연결
            small_bridge.small.start_socket()
        yield
        # 서버 종료 시 LARGE 정리(이메일 스케줄러 스레드 등). 이 함수가 없는 LARGE 버전이면 건너뜀
        shutdown = getattr(large_main, "shutdown", None)
        if shutdown:
            shutdown()

    demo = large_settings.is_demo
    app = FastAPI(
        title="총무봇 공용 서버",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/large/docs" if demo else None,
        openapi_url="/api/large/openapi.json" if demo else None,
    )

    configure_large(app)  # /api/large/*
    add_large_ui(app, large_dist)  # /large/*

    @app.get("/api/hub", include_in_schema=False)
    def hub_status():
        s = small_bridge.small
        return {
            "hub": "chongmubot",
            "small": {"api": "/api/*", "ui": "/demo/index.html", "slack_token": bool(s.TOKEN), "socket": dict(s.SOCKET), "llm_key": bool(s.LLM_KEY)},
            "large": {
                "api": "/api/large/*", "ui": "/large/", "ui_built": (large_dist / "index.html").exists(),
                "app_mode": large_settings.app_mode, "email_mode": large_settings.email_mode, "llm_mode": large_settings.llm_mode,
                "email_scheduler": getattr(large_main, "scheduler", None) and {"running": large_main.scheduler.running, "paused": large_main.scheduler.paused},
            },
        }

    @app.get("/small", include_in_schema=False)
    @app.get("/small/{rest:path}", include_in_schema=False)
    def small_ui(rest: str = ""):
        # SMALL 화면은 /demo/ 경로에서 열려야 같은 서버의 API 를 쓴다(demo/index.html 의 SERVED 판정).
        return RedirectResponse("/demo/index.html")

    # 나머지 전부(/, /demo/*, SMALL API)는 SMALL 서버 코드가 처리. 반드시 마지막에 등록.
    app.add_api_route("/{path:path}", small_bridge.small_endpoint, methods=["GET", "POST", "OPTIONS", "PUT", "PATCH", "DELETE", "HEAD"], include_in_schema=False)
    return app


app = create_app()
