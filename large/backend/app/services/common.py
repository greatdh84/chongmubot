from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..errors import not_found
from ..models import AuditLog, ChatMessage, ChatThread, ConfirmTask, Event, Participant, Team, TeamManager


def audit(db: Session, event_id: Optional[int], actor: str, action: str, target: str = "", detail: dict[str, Any] | None = None) -> None:
    db.add(AuditLog(event_id=event_id, actor=actor, action=action, target=target, detail=detail or {}))


def get_event(db: Session, event_id: int) -> Event:
    ev = db.get(Event, event_id)
    if ev is None:
        raise not_found("행사")
    return ev


def get_team_in_event(db: Session, event_id: int, team_id: int) -> Team:
    team = db.get(Team, team_id)
    if team is None or team.event_id != event_id:
        raise not_found("팀")
    return team


def get_participant_in_event(db: Session, event_id: int, participant_id: int) -> Participant:
    p = db.get(Participant, participant_id)
    if p is None or p.event_id != event_id:
        raise not_found("참가자")
    return p


def active_manager(db: Session, team_id: int) -> Optional[TeamManager]:
    return db.scalar(select(TeamManager).where(TeamManager.team_id == team_id, TeamManager.revoked_at.is_(None)))


def is_active_manager(db: Session, participant_id: int, team_id: int) -> bool:
    m = active_manager(db, team_id)
    return m is not None and m.participant_id == participant_id


def get_or_create_thread(db: Session, participant: Participant, role: str) -> ChatThread:
    th = db.scalar(select(ChatThread).where(ChatThread.participant_id == participant.id, ChatThread.role == role))
    if th is None:
        th = ChatThread(event_id=participant.event_id, participant_id=participant.id, role=role, team_id=participant.team_id)
        db.add(th)
        db.flush()
    return th


def add_message(db: Session, thread: ChatThread, sender: str, text: str, meta: dict[str, Any] | None = None) -> ChatMessage:
    msg = ChatMessage(thread_id=thread.id, sender=sender, text=text, meta=meta or {})
    db.add(msg)
    thread.updated_at = clock.now()
    db.flush()
    return msg


def create_task(db: Session, *, dedupe_key: str, **fields: Any) -> tuple[ConfirmTask, bool]:
    """같은 dedupe_key 의 과제가 있으면 새로 만들지 않는다."""
    existing = db.scalar(select(ConfirmTask).where(ConfirmTask.dedupe_key == dedupe_key))
    if existing is not None:
        return existing, False
    task = ConfirmTask(dedupe_key=dedupe_key, **fields)
    db.add(task)
    db.flush()
    return task, True


def won(amount: Optional[int]) -> str:
    return "미확정" if amount is None else f"{amount:,}원"
