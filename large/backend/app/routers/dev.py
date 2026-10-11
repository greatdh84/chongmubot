"""시연 전용 API: 개발용 수신함, 역할 전환. APP_MODE=production 에서는 라우터 자체를 등록하지 않는다."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..config import API_PREFIX, settings
from ..db import get_db
from ..errors import invalid, not_found
from ..models import Admin, ChangeLog, DevMail, Participant, Team, TeamManager
from ..schemas import DevSwitch
from ..security import create_session
from ..services.common import audit, is_active_manager
from .util import set_session_cookie

router = APIRouter(prefix=API_PREFIX + "/dev", tags=["dev (demo only)"])


@router.get("/inbox")
def inbox(to: str | None = None, db: Session = Depends(get_db)):
    q = select(DevMail)
    if to:
        q = q.where(DevMail.to_email == to.strip().lower())
    return [
        {"id": m.id, "to_email": m.to_email, "subject": m.subject, "body": m.body, "created_at": clock.fmt(m.created_at)}
        for m in db.scalars(q.order_by(DevMail.id.desc()).limit(200))
    ]


@router.get("/people")
def people(db: Session = Depends(get_db)):
    """역할 전환 패널에 보여줄 시연 인물(담당자 + 시나리오가 걸린 참가자)."""
    teams = {t.id: t for t in db.scalars(select(Team))}
    managers = [
        {"participant_id": p.id, "name": p.name, "team": teams[p.team_id].code + " " + teams[p.team_id].name}
        for m, p in db.execute(select(TeamManager, Participant).join(Participant, Participant.id == TeamManager.participant_id).where(TeamManager.revoked_at.is_(None)).order_by(Participant.team_id))
    ]
    with_changes = set(db.scalars(select(ChangeLog.participant_id).where(ChangeLog.after_deadline.is_(True))))
    sample = [
        {"participant_id": p.id, "name": p.name, "team": teams[p.team_id].code, "email": p.email, "has_change": p.id in with_changes}
        for p in db.scalars(select(Participant).order_by(Participant.employee_no).limit(24))
    ]
    return {"managers": managers, "participants": sample}


@router.post("/switch")
def switch(body: DevSwitch, response: Response, db: Session = Depends(get_db)):
    if body.role == "admin":
        admin = db.scalar(select(Admin).where(Admin.email == settings.admin_email))
        if admin is None:
            raise not_found("시연 총무 계정")
        token = create_session(db, "admin", via="demo_switch", admin_id=admin.id)
        actor = f"admin:{admin.id}"
        event_id = None
    else:
        if body.participant_id is None:
            raise invalid("PARTICIPANT_REQUIRED", "participant_id 가 필요합니다")
        p = db.get(Participant, body.participant_id)
        if p is None:
            raise not_found("참가자")
        if body.role == "manager" and not is_active_manager(db, p.id, p.team_id):
            raise invalid("NOT_MANAGER", "총무가 지정한 담당자만 담당자 화면으로 전환할 수 있습니다")
        token = create_session(db, body.role, via="demo_switch", event_id=p.event_id, participant_id=p.id, team_id=p.team_id)
        actor, event_id = f"{body.role}:{p.id}", p.event_id
    audit(db, event_id, actor, "demo_role_switch")
    db.commit()
    set_session_cookie(response, body.role, token)
    return {"ok": True, "role": body.role}
