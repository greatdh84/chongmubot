"""팀 담당자 화면 API. 세션의 team_id 로만 조회한다(이메일 인증으로 발급된 세션)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..agents import communication
from ..agents.tools import ToolContext, run_tool, team_counts
from ..config import API_PREFIX
from ..db import get_db
from ..deps import ManagerCtx, current_manager
from ..errors import AppError, conflict
from ..models import ChangeLog, ConfirmTask, Participant, Settlement
from ..schemas import ChatSend, CorrectionIn, TaskAnswer
from ..services.common import add_message, audit, get_or_create_thread
from ..services.settlement import compute_team
from .util import change_out, event_out, task_out

router = APIRouter(prefix=API_PREFIX + "/team", tags=["manager"])


def _settlement_out(s: Settlement | None) -> dict | None:
    if s is None:
        return None
    return {
        "id": s.id, "version": s.version, "status": s.status, "final_fee": s.final_fee, "received": s.received,
        "refunds_paid": s.refunds_paid, "balance": s.balance, "computed_at": clock.fmt(s.computed_at),
        "confirmed_at": clock.fmt(s.confirmed_at), "manager_ack_at": clock.fmt(s.manager_ack_at), "detail": s.detail,
    }


@router.get("")
def team_overview(ctx: ManagerCtx = Depends(current_manager), db: Session = Depends(get_db)):
    calc = compute_team(db, ctx.event, ctx.team)
    names = {p.id: p.name for p in db.scalars(select(Participant).where(Participant.team_id == ctx.team.id))}
    changes = db.scalars(select(ChangeLog).where(ChangeLog.participant_id.in_(names)).order_by(ChangeLog.id.desc()))
    tasks = db.scalars(select(ConfirmTask).where(ConfirmTask.team_id == ctx.team.id, ConfirmTask.assignee == "manager").order_by(ConfirmTask.id.desc()))
    confirmed = db.scalar(select(Settlement).where(Settlement.team_id == ctx.team.id, Settlement.status == "confirmed"))
    return {
        "manager": {"id": ctx.participant.id, "name": ctx.participant.name},
        "team": {"id": ctx.team.id, "code": ctx.team.code, "name": ctx.team.name},
        "event": event_out(ctx.event),
        "counts": team_counts(db, ctx.team.id),
        "billing": calc,
        "changes": [change_out(c, names.get(c.participant_id)) for c in changes],
        "tasks": [task_out(t) for t in tasks],
        "confirmed_settlement": _settlement_out(confirmed),
    }


def _ctx(db: Session, ctx: ManagerCtx, message_id: int | None = None) -> ToolContext:
    thread = get_or_create_thread(db, ctx.participant, "manager")
    return ToolContext(db=db, event=ctx.event, actor=ctx.participant, role="manager", thread=thread, message_id=message_id)


@router.post("/tasks/{task_id}/answer")
def answer(task_id: int, body: TaskAnswer, ctx: ManagerCtx = Depends(current_manager), db: Session = Depends(get_db)):
    """화면 버튼으로 하는 명시적 답변. 채팅 답변과 같은 도구(answer_task)를 쓴다."""
    tctx = _ctx(db, ctx)
    task = db.get(ConfirmTask, task_id)
    label = next((o["label"] for o in (task.options if task else []) if o["key"] == body.option), body.option)
    msg = add_message(db, tctx.thread, "user", f"[확인 요청 #{task_id} 답변] {label}" + (f" — {body.note}" if body.note else ""))
    tctx.message_id = msg.id
    step = run_tool(tctx, "answer_task", {"task_id": task_id, "option": body.option, "note": body.note})
    if not step["ok"]:
        db.rollback()
        err = step["error"]
        raise AppError(404 if err["code"] == "NOT_FOUND" else 409 if err["code"] == "TASK_NOT_OPEN" else 422, err["code"], err["message"])
    add_message(db, tctx.thread, "agent", f"답변을 기록했어요. 정산 Agent 재검토 결과: {step['result']['review']['summary']}", {"steps": [step]})
    db.commit()
    return step["result"]


@router.post("/corrections")
def correction(body: CorrectionIn, ctx: ManagerCtx = Depends(current_manager), db: Session = Depends(get_db)):
    tctx = _ctx(db, ctx)
    msg = add_message(db, tctx.thread, "user", f"[정정 요청] {body.text}")
    tctx.message_id = msg.id
    step = run_tool(tctx, "request_correction", {"text": body.text})
    db.commit()
    return step["result"]


@router.post("/statement/ack")
def ack(ctx: ManagerCtx = Depends(current_manager), db: Session = Depends(get_db)):
    s = db.scalar(select(Settlement).where(Settlement.team_id == ctx.team.id, Settlement.status == "confirmed"))
    if s is None:
        raise conflict("SETTLEMENT_NOT_CONFIRMED", "총무가 확정한 정산 명세가 아직 없습니다")
    s.manager_ack_at, s.manager_ack_by = clock.now(), ctx.participant.id
    audit(db, ctx.event.id, f"manager:{ctx.participant.id}", "statement_acknowledged", f"settlement:{s.id}")
    db.commit()
    return {"ok": True, "manager_ack_at": clock.fmt(s.manager_ack_at)}


@router.get("/chat")
def get_chat(ctx: ManagerCtx = Depends(current_manager), db: Session = Depends(get_db)):
    out = communication.thread_for(db, ctx.participant, "manager")
    db.commit()
    return out


@router.post("/chat")
def send_chat(body: ChatSend, ctx: ManagerCtx = Depends(current_manager), db: Session = Depends(get_db)):
    out = communication.handle_message(db, ctx.event, ctx.participant, "manager", body.text)
    db.commit()
    return out
