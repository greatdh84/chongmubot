"""서버 설정. 모든 비밀값(Slack 토큰, LLM 키, 관리자 비밀번호)은 환경변수로만 받는다.

연락 경로는 Slack DM 으로 통일했다(소모임과 같은 Slack 봇·같은 토큰).
- NOTIFY_MODE=mock(기본): 실제로 보내지 않고 개발용 수신함에만 기록
- NOTIFY_MODE=slack     : 저장소 루트 .env 의 SLACK_BOT_TOKEN 으로 실제 DM 발송
LLM: LLM_MODE=mock(기본) | claude(루트 .env 의 LLM_API_KEY, 소모임과 같은 키) | gemini
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent.parent  # 총무봇 저장소 루트(소모임 .env 가 있는 곳)
SHARED_KEYS = ("SLACK_BOT_TOKEN", "LLM_API_KEY", "SLACK_SIGNING_SECRET")  # 소모임과 같이 쓰는 비밀값: 루트 .env 에서도 읽는다
DEFAULT_SECRET = "dev-only-insecure-secret"
# 공용 서버(총무봇 hub)에서 SMALL API 와 섞이지 않도록 LARGE 의 API·화면은 각각 이 경로 아래에만 둔다.
API_PREFIX = "/api/large"
UI_PREFIX = "/large"


def _read_env_file(path: Path, only: tuple[str, ...] | None = None) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if only is not None and key not in only:
            continue
        if value:  # 빈 값은 '설정 안 함'으로 본다(.env.example 을 그대로 복사해도 기본값 유지)
            os.environ.setdefault(key, value)


def _load_dotenv() -> None:
    """backend/.env 를 읽고, 소모임과 같이 쓰는 키(Slack 봇 토큰·Claude 키)는 저장소 루트 .env 에서도 읽는다.
    이미 설정된 환경변수는 덮어쓰지 않는다."""
    _read_env_file(BACKEND_DIR / ".env")
    if os.environ.get("LARGE_SKIP_ROOT_ENV") != "1":  # 테스트는 루트 키를 읽지 않음
        _read_env_file(REPO_ROOT / ".env", SHARED_KEYS)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Settings:
    app_mode: str = "demo"  # demo | production
    email_mode: str = "mock"  # 알림 발송: mock | slack (이전 이름 유지: 실제 의미는 notify_mode)
    llm_mode: str = "mock"  # mock | claude | gemini
    database_url: str = ""
    secret_key: str = DEFAULT_SECRET
    admin_email: str = ""
    admin_password: str = ""
    public_base_url: str = "http://127.0.0.1:8787/large"
    timezone: str = "Asia/Seoul"
    quiet_start_hour: int = 21  # 21시 ~ 다음날 8시는 발송 금지
    quiet_end_hour: int = 8
    daily_limit: int = 2  # 수신자별 하루 최대 안내/독촉 DM 수(인증코드 제외)
    email_scheduler: bool = True  # 발송 큐 자동 실행(서버 안 백그라운드 스레드)
    email_scheduler_interval: int = 60  # 초
    invite_ttl_days: int = 30
    session_ttl_hours: int = 12
    slack_bot_token: str = ""
    # 슬랙 실시간 연동(Events API → POST /api/large/slack/events). 기본 꺼짐. 켜도 서명 비밀값이 없으면 모든 요청 403
    slack_events: bool = False
    slack_signing_secret: str = ""
    claude_api_key: str = ""
    claude_model: str = "claude-haiku-5-5"
    gemini_api_key: str = ""
    gemini_models: list[str] = field(default_factory=lambda: ["gemini-flash-latest", "gemini-flash-lite-latest"])

    @property
    def is_demo(self) -> bool:
        return self.app_mode == "demo"

    @property
    def cookie_secure(self) -> bool:
        return self.app_mode == "production"

    def validate(self) -> None:
        if self.app_mode not in ("demo", "production"):
            raise RuntimeError("APP_MODE 는 demo 또는 production 이어야 합니다")
        if self.email_mode not in ("mock", "slack"):
            raise RuntimeError("NOTIFY_MODE 는 mock 또는 slack 이어야 합니다")
        if self.llm_mode not in ("mock", "claude", "gemini"):
            raise RuntimeError("LLM_MODE 는 mock, claude, gemini 중 하나여야 합니다")
        if self.app_mode == "production":
            if self.secret_key == DEFAULT_SECRET or len(self.secret_key) < 32:
                raise RuntimeError("운영 모드에는 32자 이상의 SECRET_KEY 가 필요합니다")
            if not self.admin_email or not self.admin_password:
                raise RuntimeError("운영 모드에는 ADMIN_EMAIL / ADMIN_PASSWORD 가 필요합니다")
        if self.email_mode == "slack" and not self.slack_bot_token:
            raise RuntimeError("NOTIFY_MODE=slack 에는 SLACK_BOT_TOKEN(저장소 루트 .env)이 필요합니다")
        if self.llm_mode == "claude" and not self.claude_api_key:
            raise RuntimeError("LLM_MODE=claude 에는 LLM_API_KEY(저장소 루트 .env)가 필요합니다")
        if self.llm_mode == "gemini" and not self.gemini_api_key:
            raise RuntimeError("LLM_MODE=gemini 에는 GEMINI_API_KEY 가 필요합니다")


def load_settings() -> Settings:
    _load_dotenv()
    app_mode = _env("APP_MODE", "demo")
    s = Settings(
        app_mode=app_mode,
        email_mode=_env("NOTIFY_MODE", _env("EMAIL_MODE", "mock")),
        llm_mode=_env("LLM_MODE", "mock"),
        database_url=_env("DATABASE_URL", f"sqlite:///{BACKEND_DIR / 'var' / 'chongmu.db'}"),
        secret_key=_env("SECRET_KEY", DEFAULT_SECRET),
        admin_email=_env("ADMIN_EMAIL", "admin@example.com" if app_mode == "demo" else ""),
        admin_password=_env("ADMIN_PASSWORD", "demo1234!" if app_mode == "demo" else ""),
        public_base_url=_env("PUBLIC_BASE_URL", "http://127.0.0.1:8787/large").rstrip("/"),
        daily_limit=int(_env("EMAIL_DAILY_LIMIT", "2")),
        quiet_start_hour=int(_env("EMAIL_QUIET_START", "21")),
        quiet_end_hour=int(_env("EMAIL_QUIET_END", "8")),
        slack_bot_token=_env("SLACK_BOT_TOKEN"),
        slack_events=_env("SLACK_EVENTS", "off").lower() in ("on", "1", "true", "yes"),
        slack_signing_secret=_env("SLACK_SIGNING_SECRET"),
        claude_api_key=_env("LLM_API_KEY"),
        claude_model=_env("CLAUDE_MODEL", "claude-haiku-5-5"),
        gemini_api_key=_env("GEMINI_API_KEY"),
    )
    email_mode = s.email_mode
    # 기본: 모의 발송이면 켜고, 실제 Slack 이면 NOTIFY_SCHEDULER=on 으로 명시할 때만 켠다(수업 워크스페이스에 실수로 DM 이 나가지 않게)
    s.email_scheduler = _env("NOTIFY_SCHEDULER", _env("EMAIL_SCHEDULER", "on" if email_mode == "mock" else "off")).lower() in ("on", "1", "true", "yes")
    s.email_scheduler_interval = max(10, int(_env("EMAIL_SCHEDULER_INTERVAL", "60")))
    if _env("GEMINI_MODEL"):
        s.gemini_models = [_env("GEMINI_MODEL")] + [m for m in s.gemini_models if m != _env("GEMINI_MODEL")]
    s.validate()
    return s


settings = load_settings()
