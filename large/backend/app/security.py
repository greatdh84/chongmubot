"""토큰·비밀번호·세션 유틸. 토큰 원문은 DB와 로그 어디에도 남기지 않는다."""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import clock
from .config import settings
from .models import AuthSession

COOKIE_NAMES = {"admin": "cm_admin", "participant": "cm_participant", "manager": "cm_manager"}


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hmac.new(settings.secret_key.encode(), token.encode(), hashlib.sha256).hexdigest()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
    return hmac.compare_digest(digest.hex(), digest_hex)


def new_code() -> str:
    return f"{secrets.randbelow(10**6):06d}"


def create_session(db: Session, role: str, *, via: str, event_id=None, admin_id=None, participant_id=None, team_id=None) -> str:
    token = new_token()
    db.add(
        AuthSession(
            token_hash=hash_token(token),
            role=role,
            via=via,
            event_id=event_id,
            admin_id=admin_id,
            participant_id=participant_id,
            team_id=team_id,
            expires_at=clock.now() + timedelta(hours=settings.session_ttl_hours),
        )
    )
    return token


def find_session(db: Session, role: str, token: str | None) -> AuthSession | None:
    if not token:
        return None
    s = db.scalar(select(AuthSession).where(AuthSession.token_hash == hash_token(token), AuthSession.role == role))
    if s is None or s.revoked_at is not None or s.expires_at <= clock.now():
        return None
    return s


_TOKEN_PATTERNS = [
    re.compile(r"((?:token|code)[\"']?\s*[=:]\s*[\"']?)[A-Za-z0-9_\-]{6,}"),
    re.compile(r"(#t=)[A-Za-z0-9_\-]+"),
]


def redact(text: str) -> str:
    for pat in _TOKEN_PATTERNS:
        text = pat.sub(r"\1[REDACTED]", text)
    return text


class RedactSecretsFilter(logging.Filter):
    """혹시라도 로그 메시지에 토큰 형태가 섞이면 가린다(방어용)."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        redacted = redact(msg)
        if redacted != msg:
            record.msg, record.args = redacted, ()
        return True
