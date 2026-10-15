"""환불 안내 DM 의 '환불 받을 계좌 입력' 개인 링크(로그인 없이 토큰으로, 담당자 전용 · 7일).
토큰은 URL fragment(#t=…)로만 오가고, 화면이 POST 본문으로 보낸다(서버 접근 로그에 남지 않음)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import clock
from ..config import API_PREFIX, settings
from ..db import get_db
from ..models import Event, Participant, Team
from ..schemas import RefundLinkIn, RefundLinkSubmit
from ..services import refunds
from ..services.settlement import compute_team

router = APIRouter(prefix=API_PREFIX + "/refund-link", tags=["refund link"])


@router.post("/open")
def open_link(body: RefundLinkIn, db: Session = Depends(get_db)):
    ln = refunds.resolve_link(db, body.token)
    team, p, ev = db.get(Team, ln.team_id), db.get(Participant, ln.participant_id), db.get(Event, ln.event_id)
    calc = compute_team(db, ev, team)
    return {
        "event": ev.name, "team": team.name, "manager": p.name, "due": max(0, -calc["balance"]), "expires_at": clock.fmt(ln.expires_at),
        "account": refunds.account_out(refunds.saved(db, team.id)), "demo_note": refunds.DEMO_NOTE if settings.is_demo else "",
    }


@router.post("/submit")
def submit(body: RefundLinkSubmit, db: Session = Depends(get_db)):
    ln = refunds.resolve_link(db, body.token)
    team, p, ev = db.get(Team, ln.team_id), db.get(Participant, ln.participant_id), db.get(Event, ln.event_id)
    a = refunds.submit(db, ev, team, p, body.bank, body.account_no, body.holder, source="link")
    ln.used_at = clock.now()
    db.commit()
    return {"account": refunds.account_out(a)}
