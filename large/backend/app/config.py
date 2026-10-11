"""서버 설정. 모든 비밀값(이메일 연결 정보, LLM 키, 관리자 비밀번호)은 환경변수로만 받는다."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SECRET = "dev-only-insecure-secret"


def _load_dotenv() -> None:
    """backend/.env 가 있으면 읽는다(이미 설정된 환경변수는 덮어쓰지 않음)."""
    path = BACKEND_DIR / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if value.strip():  # 빈 값은 '설정 안 함'으로 본다(.env.example 을 그대로 복사해도 기본값 유지)
            os.environ.setdefault(key.strip(), value.strip())


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Settings:
    app_mode: str = "demo"  # demo | production
    email_mode: str = "mock"  # mock | smtp
    llm_mode: str = "mock"  # mock | gemini
    database_url: str = ""
    secret_key: str = DEFAULT_SECRET
    admin_email: str = ""
    admin_password: str = ""
    public_base_url: str = "http://localhost:5173"
    timezone: str = "Asia/Seoul"
    quiet_start_hour: int = 21  # 21시 ~ 다음날 8시는 발송 금지
    quiet_end_hour: int = 8
    daily_limit: int = 2  # 수신자별 하루 최대 안내/독촉 메일 수(인증코드 제외)
    invite_ttl_days: int = 30
    session_ttl_hours: int = 12
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
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
        if self.email_mode not in ("mock", "smtp"):
            raise RuntimeError("EMAIL_MODE 는 mock 또는 smtp 이어야 합니다")
        if self.llm_mode not in ("mock", "gemini"):
            raise RuntimeError("LLM_MODE 는 mock 또는 gemini 이어야 합니다")
        if self.app_mode == "production":
            if self.secret_key == DEFAULT_SECRET or len(self.secret_key) < 32:
                raise RuntimeError("운영 모드에는 32자 이상의 SECRET_KEY 가 필요합니다")
            if not self.admin_email or not self.admin_password:
                raise RuntimeError("운영 모드에는 ADMIN_EMAIL / ADMIN_PASSWORD 가 필요합니다")
        if self.email_mode == "smtp" and not (self.smtp_host and self.smtp_from):
            raise RuntimeError("EMAIL_MODE=smtp 에는 SMTP_HOST / SMTP_FROM 이 필요합니다")
        if self.llm_mode == "gemini" and not self.gemini_api_key:
            raise RuntimeError("LLM_MODE=gemini 에는 GEMINI_API_KEY 가 필요합니다")


def load_settings() -> Settings:
    _load_dotenv()
    app_mode = _env("APP_MODE", "demo")
    s = Settings(
        app_mode=app_mode,
        email_mode=_env("EMAIL_MODE", "mock"),
        llm_mode=_env("LLM_MODE", "mock"),
        database_url=_env("DATABASE_URL", f"sqlite:///{BACKEND_DIR / 'var' / 'chongmu.db'}"),
        secret_key=_env("SECRET_KEY", DEFAULT_SECRET),
        admin_email=_env("ADMIN_EMAIL", "admin@example.com" if app_mode == "demo" else ""),
        admin_password=_env("ADMIN_PASSWORD", "demo1234!" if app_mode == "demo" else ""),
        public_base_url=_env("PUBLIC_BASE_URL", "http://localhost:5173").rstrip("/"),
        daily_limit=int(_env("EMAIL_DAILY_LIMIT", "2")),
        quiet_start_hour=int(_env("EMAIL_QUIET_START", "21")),
        quiet_end_hour=int(_env("EMAIL_QUIET_END", "8")),
        smtp_host=_env("SMTP_HOST"),
        smtp_port=int(_env("SMTP_PORT", "587")),
        smtp_user=_env("SMTP_USER"),
        smtp_password=_env("SMTP_PASSWORD"),
        smtp_from=_env("SMTP_FROM"),
        gemini_api_key=_env("GEMINI_API_KEY"),
    )
    if _env("GEMINI_MODEL"):
        s.gemini_models = [_env("GEMINI_MODEL")] + [m for m in s.gemini_models if m != _env("GEMINI_MODEL")]
    s.validate()
    return s


settings = load_settings()
