"""참가자 본인 화면 API. 세션의 participant_id 로만 조회·변경한다(경로에 ID 를 받지 않음)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agents import communication
from ..config import API_PREFIX
from ..db import get_db
from ..deps import ParticipantCtx, current_participant
from ..models import CHOICE_LABELS, ChangeLog, Team
from ..schemas import ChatSend, ResponseSubmit
from ..services import responses as resp_svc
from ..services.common import is_active_manager
from .util import change_out, event_out

router = APIRouter(prefix=API_PREFIX + "/me", tags=["participant"])


def me_out(db: Session, ctx: ParticipantCtx) -> dict:
    p, ev = ctx.participant, ctx.event
    st = resp_svc.fee_table(db, ev, p.team_id)[p.id]
    changes = list(db.scalars(select(ChangeLog).where(ChangeLog.participant_id == p.id).order_by(ChangeLog.id.desc())))
    team = db.get(Team, p.team_id)
    masked = p.email[:2] + "***@" + p.email.split("@")[1]
    return {
        "participant": {"id": p.id, "name": p.name, "email_masked": masked, "team": {"id": team.id, "code": team.code, "name": team.name}},
        "event": event_out(ev),
        "response": {"choice": st.choice, "label": CHOICE_LABELS[st.choice], "fee": st.liability, "snapshot_choice": st.snapshot_choice},
        "pending_change": next((change_out(c) for c in changes if c.status == "pending_approval"), None),
        "changes": [change_out(c) for c in changes],
        "is_manager": is_active_manager(db, p.id, p.team_id),
    }


@router.get("")
def get_me(ctx: ParticipantCtx = Depends(current_participant), db: Session = Depends(get_db)):
    return me_out(db, ctx)


@router.post("/response/preview")
def preview(body: ResponseSubmit, ctx: ParticipantCtx = Depends(current_participant), db: Session = Depends(get_db)):
    out = resp_svc.preview_change(db, ctx.event, ctx.participant, body.choice)
    db.rollback()  # 미리보기는 아무것도 저장하지 않는다
    return out


@router.post("/response")
def submit(body: ResponseSubmit, ctx: ParticipantCtx = Depends(current_participant), db: Session = Depends(get_db)):
    change = resp_svc.submit_change(db, ctx.event, ctx.participant, body.choice, source="web", actor=f"participant:{ctx.participant.id}", note=body.note[:300])
    db.commit()
    return {"change": change_out(change), "me": me_out(db, ctx)}


@router.get("/chat")
def get_chat(ctx: ParticipantCtx = Depends(current_participant), db: Session = Depends(get_db)):
    out = communication.thread_for(db, ctx.participant, "participant")
    db.commit()
    return out


@router.post("/chat")
def send_chat(body: ChatSend, ctx: ParticipantCtx = Depends(current_participant), db: Session = Depends(get_db)):
    out = communication.handle_message(db, ctx.event, ctx.participant, "participant", body.text)
    db.commit()
    return out
