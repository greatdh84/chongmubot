"""초대 토큰: 무작위 생성, 해시 저장, 만료·재발급·폐기, 세션 교환."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .. import clock
from ..config import settings
from ..errors import AppError
from ..models import Invite, Participant
from ..security import hash_token, new_token


def issue(db: Session, participant: Participant, now: datetime | None = None) -> str:
    """새 토큰 발급. 원문은 반환만 하고(메일 렌더링용) 저장하지 않는다."""
    now = now or clock.now()
    token = new_token()
    db.add(Invite(participant_id=participant.id, token_hash=hash_token(token), expires_at=now + timedelta(days=settings.invite_ttl_days), created_at=now))
    db.flush()
    return token


def revoke_all(db: Session, participant_id: int, now: datetime | None = None) -> int:
    now = now or clock.now()
    res = db.execute(
        update(Invite).where(Invite.participant_id == participant_id, Invite.revoked_at.is_(None)).values(revoked_at=now)
    )
    return res.rowcount or 0


def link(token: str) -> str:
    # 토큰을 URL fragment(#)에 넣어 서버 접근 로그·Referer 로 새지 않게 한다.
    return f"{settings.public_base_url}/invite#t={token}"


def resolve(db: Session, token: str, now: datetime | None = None) -> Participant:
    now = now or clock.now()
    inv = db.scalar(select(Invite).where(Invite.token_hash == hash_token(token or "")))
    if inv is None:
        raise AppError(401, "INVITE_INVALID", "유효하지 않은 초대 링크입니다")
    if inv.revoked_at is not None:
        raise AppError(401, "INVITE_REVOKED", "폐기된 초대 링크입니다. 총무에게 재발급을 요청하세요")
    if inv.expires_at <= now:
        raise AppError(401, "INVITE_EXPIRED", "만료된 초대 링크입니다. 총무에게 재발급을 요청하세요")
    inv.last_used_at = now
    return db.get(Participant, inv.participant_id)


def status(db: Session, participant_id: int, now: datetime | None = None) -> dict:
    now = now or clock.now()
    invs = list(db.scalars(select(Invite).where(Invite.participant_id == participant_id)))
    active = [i for i in invs if i.revoked_at is None and i.expires_at > now]
    return {
        "issued": len(invs),
        "active": len(active),
        "latest_expires_at": clock.fmt(max((i.expires_at for i in active), default=None)),
        "last_used_at": clock.fmt(max((i.last_used_at for i in invs if i.last_used_at), default=None)),
    }
