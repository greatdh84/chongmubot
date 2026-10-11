"""역할별 접근 통제. 참가자는 본인, 담당자는 자기 팀, 총무는 자기 행사에만 접근한다."""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from .db import get_db
from .errors import forbidden, not_found, unauthorized
from .models import Admin, Event, Participant, Team
from .security import COOKIE_NAMES, find_session
from .services.common import is_active_manager


@dataclass
class ParticipantCtx:
    participant: Participant
    event: Event


@dataclass
class ManagerCtx:
    participant: Participant
    team: Team
    event: Event


def current_admin(request: Request, db: Session = Depends(get_db)) -> Admin:
    s = find_session(db, "admin", request.cookies.get(COOKIE_NAMES["admin"]))
    if s is None:
        raise unauthorized()
    return db.get(Admin, s.admin_id)


def admin_event(event_id: int, admin: Admin = Depends(current_admin), db: Session = Depends(get_db)) -> Event:
    ev = db.get(Event, event_id)
    if ev is None or ev.admin_id != admin.id:
        raise not_found("행사")  # 남의 행사는 존재 여부도 알리지 않음
    return ev


def current_participant(request: Request, db: Session = Depends(get_db)) -> ParticipantCtx:
    s = find_session(db, "participant", request.cookies.get(COOKIE_NAMES["participant"]))
    if s is None:
        raise unauthorized("초대 링크로 다시 들어와 주세요")
    p = db.get(Participant, s.participant_id)
    return ParticipantCtx(p, db.get(Event, p.event_id))


def current_manager(request: Request, db: Session = Depends(get_db)) -> ManagerCtx:
    s = find_session(db, "manager", request.cookies.get(COOKIE_NAMES["manager"]))
    if s is None:
        raise unauthorized("담당자 이메일 인증이 필요합니다")
    # 세션 발급 후 총무가 담당자를 바꿨다면 즉시 접근을 막는다.
    if not is_active_manager(db, s.participant_id, s.team_id):
        raise forbidden("더 이상 이 팀의 담당자가 아닙니다")
    p = db.get(Participant, s.participant_id)
    return ManagerCtx(p, db.get(Team, s.team_id), db.get(Event, p.event_id))
