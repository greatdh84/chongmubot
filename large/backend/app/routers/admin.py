"""총무 API. 모든 경로는 /api/admin/events/{event_id}/... 이고 admin_event 의존성으로 '자기 행사'만 허용한다.
하위 리소스(팀·참가자·거래·배정·과제)는 각 서비스에서 event_id 일치를 다시 검사한다."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import Response as RawResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import clock
from ..agents import admin_agent, settlement_agent
from ..config import API_PREFIX, settings
from ..db import get_db
from ..deps import admin_event, current_admin
from ..errors import conflict, invalid, not_found
from ..models import (
    CHOICE_LABELS, Admin, AgentRun, Allocation, AuditLog, BankTransaction, ChangeLog, ConfirmTask, EmailJob, Event,
    Participant, RefundRule, Response, Settlement, Team, TeamManager,
)
from ..schemas import AdminResponseSet, AllocationIn, BotSettings, ChatSend as ChatIn, Decision, EmailEnqueue, EventUpdate, ManagerAssign, PayoutIn, RefundRuleIn, SchedulerToggle, TaskResolve
from ..services import demo_import, emails, invites, payments, report as rpt, responses as resp_svc, roster, settlement as stl
from ..services.common import active_manager, audit, get_participant_in_event, get_team_in_event
from .util import change_out, event_out, task_out

router = APIRouter(prefix=API_PREFIX + "/admin", tags=["admin"])
MAX_UPLOAD = 2 * 1024 * 1024


def _actor(admin: Admin) -> str:
    return f"admin:{admin.id}"


def _refresh(db: Session, ev: Event, trigger: str) -> None:
    """돈·인원에 영향을 주는 처리 뒤에는 정산 Agent 가 보고서를 다시 만든다(내용이 같으면 새 버전 없음)."""
    rpt.refresh(db, ev, trigger)


async def _read_upload(file: UploadFile) -> bytes:
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise invalid("FILE_TOO_LARGE", "파일은 2MB 이하만 올릴 수 있습니다")
    return data


@router.get("/events")
def list_events(admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    return [event_out(e) for e in db.scalars(select(Event).where(Event.admin_id == admin.id).order_by(Event.id))]


@router.get("/events/{event_id}")
def dashboard(ev: Event = Depends(admin_event), db: Session = Depends(get_db)):
    if rpt.ensure_checkpoints(db, ev):
        _refresh(db, ev, "checkpoint")
        db.commit()
    pids = select(Participant.id).where(Participant.event_id == ev.id)
    total = db.scalar(select(func.count()).select_from(Participant).where(Participant.event_id == ev.id))
    counts = dict(db.execute(select(Response.choice, func.count()).where(Response.participant_id.in_(pids)).group_by(Response.choice)).all())
    counts["none"] = total - sum(counts.values())
    teams = list(db.scalars(select(Team).where(Team.event_id == ev.id)))
    calcs = [stl.compute_team(db, ev, t) for t in teams]
    txns = list(db.scalars(select(BankTransaction).where(BankTransaction.event_id == ev.id)))
    unallocated = sum(1 for t in txns if payments.allocated_sum(db, t.id) < t.amount)
    tasks = dict(db.execute(select(ConfirmTask.assignee, func.count()).where(ConfirmTask.event_id == ev.id, ConfirmTask.status.in_(("open", "answered", "escalated"))).group_by(ConfirmTask.assignee)).all())
    email_counts = dict(db.execute(select(EmailJob.status, func.count()).where(EmailJob.event_id == ev.id).group_by(EmailJob.status)).all())
    return {
        "event": event_out(ev),
        "modes": {"app": settings.app_mode, "notify": settings.email_mode, "llm": settings.llm_mode},
        "participants": total,
        "teams": len(teams),
        "teams_without_manager": sum(1 for t in teams if active_manager(db, t.id) is None),
        "response_counts": {k: counts.get(k, 0) for k in ("bus", "car", "absent", "undecided", "none")},
        "money": {
            "final_fee": sum(c["final_fee"] for c in calcs),
            "received": sum(c["received"] for c in calcs),
            "refunds_paid": sum(c["refunds_paid"] for c in calcs),
            "teams_to_collect": sum(1 for c in calcs if c["balance"] > 0),
            "teams_to_refund": sum(1 for c in calcs if c["balance"] < 0),
            "teams_blocked": sum(1 for c in calcs if c["blockers"]),
        },
        "transactions": {"total": len(txns), "unallocated": unallocated, "proposed_allocations": db.scalar(select(func.count()).select_from(Allocation).where(Allocation.event_id == ev.id, Allocation.status == "proposed"))},
        "open_tasks": tasks,
        "pending_changes": db.scalar(select(func.count()).select_from(ChangeLog).where(ChangeLog.event_id == ev.id, ChangeLog.status == "pending_approval")),
        "emails": email_counts,
        "stage": rpt.stage_of(ev), "stage_label": rpt.STAGES[rpt.stage_of(ev)],
        "report": (lambda r: {"version": r.version, "created_at": clock.fmt(r.created_at), "trigger": r.trigger} if r else None)(rpt.latest(db, ev)),
    }


# ---------------------------------------------------------------- 설정

@router.put("/events/{event_id}")
def update_event(body: EventUpdate, ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    data = body.model_dump(exclude_none=True)
    if data.pop("mode", ev.mode) != ev.mode:
        raise conflict("MODE_LOCKED", "행사의 모임 규모(small/large)는 만든 뒤 바꿀 수 없습니다")
    if ev.snapshot_taken_at and any(k in data for k in ("fee_bus", "fee_car", "fee_absent", "attend_deadline")):
        raise conflict("LOCKED_AFTER_SNAPSHOT", "마감 기준 응답을 저장한 뒤에는 회비와 참석 마감을 바꿀 수 없습니다")
    for k in ("event_at", "attend_deadline", "payment_deadline"):
        if k in data:
            data[k] = clock.parse_input(data[k])
    for k, v in data.items():
        setattr(ev, k, v)
    if ev.attend_deadline > ev.payment_deadline or ev.payment_deadline > ev.event_at:
        raise invalid("DEADLINE_ORDER", "참석 마감 ≤ 입금 마감 ≤ 행사일 순서여야 합니다")
    audit(db, ev.id, _actor(admin), "event_updated", detail={k: str(v) for k, v in data.items()})
    db.commit()
    return event_out(ev)


@router.post("/events/{event_id}/bot")
def set_bot(body: BotSettings, ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    """초기설정 화면에서 고른 총무봇 성격·독촉 전략. 문구 말투에만 쓰이고 금액·대상 판단에는 쓰지 않는다."""
    ev.bot_persona, ev.bot_strategy = body.persona, body.strategy
    audit(db, ev.id, _actor(admin), "bot_updated", detail={"persona": body.persona, "strategy": body.strategy})
    db.commit()
    return event_out(ev)


def _rule_out(r: RefundRule) -> dict:
    return {"id": r.id, "change_type": r.change_type, "valid_until": clock.fmt(r.valid_until), "refund_percent": r.refund_percent, "description": r.description}


@router.get("/events/{event_id}/refund-rules")
def list_rules(ev: Event = Depends(admin_event), db: Session = Depends(get_db)):
    return [_rule_out(r) for r in db.scalars(select(RefundRule).where(RefundRule.event_id == ev.id).order_by(RefundRule.change_type, RefundRule.valid_until))]


@router.post("/events/{event_id}/refund-rules")
def add_rule(body: RefundRuleIn, ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    r = RefundRule(event_id=ev.id, change_type=body.change_type, valid_until=clock.parse_input(body.valid_until) if body.valid_until else None, refund_percent=body.refund_percent, description=body.description)
    db.add(r)
    db.flush()
    audit(db, ev.id, _actor(admin), "refund_rule_added", f"rule:{r.id}")
    db.commit()
    return _rule_out(r)


@router.delete("/events/{event_id}/refund-rules/{rule_id}")
def delete_rule(rule_id: int, ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    r = db.get(RefundRule, rule_id)
    if r is None or r.event_id != ev.id:
        raise not_found("환불 규칙")
    if db.scalar(select(ChangeLog.id).where(ChangeLog.refund_rule_id == r.id).limit(1)):
        raise conflict("RULE_IN_USE", "이미 변경 기록에 쓰인 규칙은 지울 수 없습니다")
    db.delete(r)
    audit(db, ev.id, _actor(admin), "refund_rule_deleted", f"rule:{rule_id}")
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- 명단·팀·초대

@router.post("/events/{event_id}/roster")
async def upload_roster(file: UploadFile = File(...), ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    result = roster.import_roster(db, ev, await _read_upload(file), file.filename or "roster.csv", _actor(admin), admin_id=admin.id)
    _refresh(db, ev, "roster_upload")
    db.commit()
    return result


# 시연용: 참석 응답·마감 후 변경을 CSV 로 한 번에 재현(APP_MODE=demo 에서만. 운영에서는 없는 API)
@router.post("/events/{event_id}/demo/responses")
async def demo_responses(file: UploadFile = File(...), ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    if not settings.is_demo:
        raise not_found("API")
    out = demo_import.import_responses(db, ev, admin, await _read_upload(file))
    db.commit()
    return out


@router.post("/events/{event_id}/demo/changes")
async def demo_changes(file: UploadFile = File(...), ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    if not settings.is_demo:
        raise not_found("API")
    out = demo_import.import_changes(db, ev, admin, await _read_upload(file))
    db.commit()
    return out


@router.get("/events/{event_id}/teams")
def list_teams(ev: Event = Depends(admin_event), db: Session = Depends(get_db)):
    out = []
    for t in db.scalars(select(Team).where(Team.event_id == ev.id).order_by(Team.code)):
        calc = stl.compute_team(db, ev, t)
        m = active_manager(db, t.id)
        mp = db.get(Participant, m.participant_id) if m else None
        latest = stl.latest(db, t.id)
        out.append(
            {
                "id": t.id, "code": t.code, "name": t.name, "headcount": calc["headcount"],
                "manager": {"participant_id": mp.id, "name": mp.name, "slack_id": mp.slack_id, "phone": mp.phone} if mp else None,
                "counts": _choice_counts(db, ev, t.id),
                "final_fee": calc["final_fee"], "received": calc["received"], "refunds_paid": calc["refunds_paid"],
                "balance": calc["balance"], "balance_label": calc["balance_label"], "blockers": calc["blockers"],
                "settlement_status": latest.status if latest else None,
            }
        )
    return out


def _choice_counts(db: Session, ev: Event, team_id: int) -> dict[str, int]:
    out = {k: 0 for k in ("bus", "car", "absent", "undecided", "none")}
    for st in resp_svc.fee_table(db, ev, team_id).values():
        out[st.choice or "none"] += 1
    return out


@router.post("/events/{event_id}/teams/{team_id}/manager")
def assign_manager(team_id: int, body: ManagerAssign, ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    team = get_team_in_event(db, ev.id, team_id)
    p = get_participant_in_event(db, ev.id, body.participant_id)
    if p.team_id != team.id:
        raise invalid("NOT_TEAM_MEMBER", "담당자는 그 팀 소속 참가자 중에서만 지정할 수 있습니다")
    current = active_manager(db, team.id)
    if current and current.participant_id == p.id:
        return {"ok": True, "unchanged": True}
    if current:
        current.revoked_at = clock.now()
        db.flush()
    db.add(TeamManager(team_id=team.id, participant_id=p.id, assigned_by=admin.id))
    audit(db, ev.id, _actor(admin), "manager_assigned", f"team:{team.id}", {"participant_id": p.id, "previous": current.participant_id if current else None})
    _refresh(db, ev, "manager_assigned")
    db.commit()
    return {"ok": True}


@router.get("/events/{event_id}/participants")
def list_participants(team_id: int | None = None, q: str | None = None, ev: Event = Depends(admin_event), db: Session = Depends(get_db)):
    query = select(Participant).where(Participant.event_id == ev.id)
    if team_id:
        query = query.where(Participant.team_id == team_id)
    if q:
        query = query.where(Participant.name.contains(q) | Participant.slack_id.contains(q.upper()) | Participant.employee_no.contains(q))
    table = resp_svc.fee_table(db, ev, team_id)
    teams = {t.id: t for t in db.scalars(select(Team).where(Team.event_id == ev.id))}
    managers = {m.participant_id for m in db.scalars(select(TeamManager).where(TeamManager.revoked_at.is_(None)))}
    out = []
    for p in db.scalars(query.order_by(Participant.employee_no).limit(400)):
        st = table.get(p.id)
        out.append(
            {
                "id": p.id, "employee_no": p.employee_no, "name": p.name, "slack_id": p.slack_id, "phone": p.phone,
                "team": {"id": p.team_id, "code": teams[p.team_id].code, "name": teams[p.team_id].name},
                "is_manager": p.id in managers, "choice": st.choice if st else None, "choice_label": CHOICE_LABELS[st.choice if st else None],
                "snapshot_choice": st.snapshot_choice if st else None, "fee": st.liability if st else None,
                "pending_change_id": st.pending_change_id if st else None, "invite": invites.status(db, p.id),
            }
        )
    return out


@router.post("/events/{event_id}/participants/{participant_id}/invite/reissue")
def reissue_invite(participant_id: int, ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    """기존 링크를 모두 폐기하고 새 초대 DM 을 보낸다(모의 모드는 개발용 수신함)."""
    p = get_participant_in_event(db, ev.id, participant_id)
    revoked = invites.revoke_all(db, p.id)
    job = emails.enqueue_reissue(db, ev, p)
    outcome = emails._dispatch_one(db, job, clock.now())
    audit(db, ev.id, _actor(admin), "invite_reissued", f"participant:{p.id}", {"revoked": revoked, "email": outcome})
    db.commit()
    return {"revoked": revoked, "email_status": job.status, "note": job.note}


@router.post("/events/{event_id}/participants/{participant_id}/invite/revoke")
def revoke_invite(participant_id: int, ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    p = get_participant_in_event(db, ev.id, participant_id)
    revoked = invites.revoke_all(db, p.id)
    audit(db, ev.id, _actor(admin), "invite_revoked", f"participant:{p.id}", {"revoked": revoked})
    db.commit()
    return {"revoked": revoked}


@router.post("/events/{event_id}/participants/{participant_id}/response")
def admin_set_response(participant_id: int, body: AdminResponseSet, ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    """총무가 확인한 응답을 대신 기록(예: 전화로 확인). 마감 후면 같은 규칙으로 계산해 즉시 승인한다."""
    p = get_participant_in_event(db, ev.id, participant_id)
    change = resp_svc.submit_change(db, ev, p, body.choice, source="admin", actor=_actor(admin), note=body.note[:300])
    if change.status == "pending_approval" and not change.rule_missing:
        # 규칙으로 금액이 정해지는 변경은 바로 승인. 규칙이 없는 변경(예: 행사 당일 불참)은 '변경 승인'에서 총무가 환불액을 정한다.
        resp_svc.decide_change(db, ev, change, admin, approve=True, note="총무 직접 입력")
    _refresh(db, ev, "admin_response")
    db.commit()
    return change_out(change, p.name)


@router.post("/events/{event_id}/snapshot")
def take_snapshot(ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    if clock.now() < ev.attend_deadline:
        raise conflict("BEFORE_DEADLINE", "참석 마감 전에는 마감 기준 응답을 저장할 수 없습니다")
    created = resp_svc.ensure_snapshot(db, ev)
    audit(db, ev.id, _actor(admin), "snapshot_requested", detail={"created": created})
    _refresh(db, ev, "attend_snapshot")
    db.commit()
    return {"created": created, "snapshot_taken_at": clock.fmt(ev.snapshot_taken_at)}


# ---------------------------------------------------------------- 변경 승인

@router.get("/events/{event_id}/changes")
def list_changes(status: str | None = None, ev: Event = Depends(admin_event), db: Session = Depends(get_db)):
    q = select(ChangeLog, Participant.name).join(Participant, Participant.id == ChangeLog.participant_id).where(ChangeLog.event_id == ev.id)
    if status:
        q = q.where(ChangeLog.status == status)
    return [change_out(c, name) for c, name in db.execute(q.order_by(ChangeLog.id.desc()).limit(300))]


def _get_change(db: Session, ev: Event, change_id: int) -> ChangeLog:
    c = db.get(ChangeLog, change_id)
    if c is None or c.event_id != ev.id:
        raise not_found("변경 요청")
    return c


@router.post("/events/{event_id}/changes/{change_id}/approve")
def approve_change(change_id: int, body: Decision, ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    c = resp_svc.decide_change(db, ev, _get_change(db, ev, change_id), admin, approve=True, note=body.note, refund_amount=body.refund_amount)
    _refresh(db, ev, "change_approved")
    db.commit()
    return change_out(c)


@router.post("/events/{event_id}/changes/{change_id}/reject")
def reject_change(change_id: int, body: Decision, ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    c = resp_svc.decide_change(db, ev, _get_change(db, ev, change_id), admin, approve=False, note=body.note)
    _refresh(db, ev, "change_rejected")
    db.commit()
    return change_out(c)


# ---------------------------------------------------------------- 입금

@router.post("/events/{event_id}/transactions")
async def upload_transactions(file: UploadFile = File(...), ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    result = payments.import_transactions(db, ev, await _read_upload(file), file.filename or "transactions.csv", _actor(admin))
    _refresh(db, ev, "transactions_upload")
    db.commit()
    return result


@router.get("/events/{event_id}/transactions")
def list_transactions(ev: Event = Depends(admin_event), db: Session = Depends(get_db)):
    teams = {t.id: t for t in db.scalars(select(Team).where(Team.event_id == ev.id))}
    names = {p.id: p.name for p in db.scalars(select(Participant).where(Participant.event_id == ev.id))}
    allocs: dict[int, list[Allocation]] = {}
    for a in db.scalars(select(Allocation).where(Allocation.event_id == ev.id).order_by(Allocation.id)):
        allocs.setdefault(a.transaction_id, []).append(a)
    open_tasks: dict[int, list[int]] = {}
    for t in db.scalars(select(ConfirmTask).where(ConfirmTask.event_id == ev.id, ConfirmTask.transaction_id.is_not(None), ConfirmTask.status.in_(("open", "answered", "escalated")))):
        open_tasks.setdefault(t.transaction_id, []).append(t.id)
    out = []
    for t in db.scalars(select(BankTransaction).where(BankTransaction.event_id == ev.id).order_by(BankTransaction.txn_at, BankTransaction.id)):
        al = allocs.get(t.id, [])
        used = sum(a.amount for a in al if a.status != "cancelled")
        out.append(
            {
                "id": t.id, "txn_at": clock.fmt(t.txn_at), "depositor": t.depositor, "amount": t.amount, "memo": t.memo,
                "allocated": used, "remaining": t.amount - used, "open_task_ids": open_tasks.get(t.id, []),
                "allocations": [
                    {
                        "id": a.id, "team_id": a.team_id, "team_code": teams[a.team_id].code, "participant_id": a.participant_id,
                        "participant_name": names.get(a.participant_id), "amount": a.amount, "status": a.status, "source": a.source,
                        "confidence": a.confidence, "basis": a.basis,
                    }
                    for a in al
                ],
            }
        )
    return out


@router.post("/events/{event_id}/transactions/{transaction_id}/allocations")
def create_allocation(transaction_id: int, body: AllocationIn, ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    a = payments.allocate(
        db, ev, transaction_id, body.team_id, body.amount, participant_id=body.participant_id,
        status="confirmed" if body.confirm else "proposed", source="admin", basis=body.basis, actor=_actor(admin), admin_id=admin.id,
    )
    _refresh(db, ev, "allocation_manual")
    db.commit()
    return {"id": a.id, "status": a.status}


@router.post("/events/{event_id}/allocations/{allocation_id}/confirm")
def confirm_allocation(allocation_id: int, ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    a = payments.confirm_allocation(db, ev, allocation_id, admin.id)
    _refresh(db, ev, "allocation_confirmed")
    db.commit()
    return {"id": a.id, "status": a.status}


@router.post("/events/{event_id}/allocations/confirm-high")
def confirm_high(ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    """신뢰도 high(팀코드 + 팀원 명의) 제안만 한 번에 확정. 나머지는 하나씩 검토."""
    ids = list(db.scalars(select(Allocation.id).where(Allocation.event_id == ev.id, Allocation.status == "proposed", Allocation.confidence == "high")))
    for aid in ids:
        payments.confirm_allocation(db, ev, aid, admin.id)
    _refresh(db, ev, "allocations_confirmed_high")
    db.commit()
    return {"confirmed": len(ids)}


@router.post("/events/{event_id}/allocations/{allocation_id}/cancel")
def cancel_allocation(allocation_id: int, ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    a = payments.cancel_allocation(db, ev, allocation_id, _actor(admin))
    _refresh(db, ev, "allocation_cancelled")
    db.commit()
    return {"id": a.id, "status": a.status}


# ---------------------------------------------------------------- 정산 Agent·확인 과제

@router.post("/events/{event_id}/agent/settlement-review")
def run_settlement_review(ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    result = settlement_agent.review_event(db, ev, trigger=f"manual:{_actor(admin)}")
    _refresh(db, ev, "settlement_review")
    db.commit()
    return result


@router.get("/events/{event_id}/tasks")
def list_tasks(status: str | None = None, assignee: str | None = None, ev: Event = Depends(admin_event), db: Session = Depends(get_db)):
    q = select(ConfirmTask).where(ConfirmTask.event_id == ev.id)
    if status == "active":
        q = q.where(ConfirmTask.status.in_(("open", "answered", "escalated")))
    elif status:
        q = q.where(ConfirmTask.status == status)
    if assignee:
        q = q.where(ConfirmTask.assignee == assignee)
    return [task_out(t) for t in db.scalars(q.order_by(ConfirmTask.id.desc()).limit(300))]


@router.post("/events/{event_id}/tasks/{task_id}/resolve")
def resolve_task(task_id: int, body: TaskResolve, ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    t = db.get(ConfirmTask, task_id)
    if t is None or t.event_id != ev.id:
        raise not_found("확인 과제")
    if t.kind == "change_approval":
        raise conflict("USE_CHANGE_APPROVAL", "변경 승인 과제는 변경 요청 승인/반려로 처리하세요")
    if t.status in ("resolved", "cancelled"):
        raise conflict("ALREADY_RESOLVED", "이미 처리된 과제입니다")
    t.status, t.resolution, t.updated_at = "resolved", f"총무 처리: {body.resolution}", clock.now()
    audit(db, ev.id, _actor(admin), "task_resolved", f"task:{t.id}")
    _refresh(db, ev, "task_resolved")
    db.commit()
    return task_out(t)


@router.get("/events/{event_id}/agent-runs")
def list_runs(agent: str | None = None, ev: Event = Depends(admin_event), db: Session = Depends(get_db)):
    q = select(AgentRun).where(AgentRun.event_id == ev.id)
    if agent:
        q = q.where(AgentRun.agent == agent)
    return [
        {"id": r.id, "agent": r.agent, "trigger": r.trigger, "thread_id": r.thread_id, "evidence_message_id": r.evidence_message_id, "steps": r.steps, "rules": r.rules, "result": r.result, "llm_mode": r.llm_mode, "created_at": clock.fmt(r.created_at)}
        for r in db.scalars(q.order_by(AgentRun.id.desc()).limit(50))
    ]


# ---------------------------------------------------------------- 정산

@router.get("/events/{event_id}/settlements/{team_id}")
def team_settlement(team_id: int, ev: Event = Depends(admin_event), db: Session = Depends(get_db)):
    team = get_team_in_event(db, ev.id, team_id)
    latest = stl.latest(db, team.id)
    return {"calc": stl.compute_team(db, ev, team), "latest": {"id": latest.id, "version": latest.version, "status": latest.status, "balance": latest.balance, "confirmed_at": clock.fmt(latest.confirmed_at), "manager_ack_at": clock.fmt(latest.manager_ack_at)} if latest else None}


@router.post("/events/{event_id}/settlements/compute")
def compute_all(ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    saved = [stl.save_draft(db, ev, t) for t in db.scalars(select(Team).where(Team.event_id == ev.id))]
    audit(db, ev.id, _actor(admin), "settlements_computed", detail={"teams": len(saved)})
    db.commit()
    return {"drafts": len(saved), "blocked": sum(1 for s in saved if s.blockers)}


@router.post("/events/{event_id}/settlements/{team_id}/confirm")
def confirm_settlement(team_id: int, ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    s = stl.confirm(db, ev, team_id, admin)
    _refresh(db, ev, "settlement_confirmed")
    db.commit()
    return {"id": s.id, "version": s.version, "status": s.status, "balance": s.balance}


@router.post("/events/{event_id}/settlements/{team_id}/payouts")
def record_payout(team_id: int, body: PayoutIn, ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    p = stl.record_payout(db, ev, team_id, body.amount, body.memo, admin)
    _refresh(db, ev, "payout_recorded")
    db.commit()
    return {"id": p.id, "amount": p.amount}


# ---------------------------------------------------------------- 정산 보고서·총무 Agent

@router.get("/events/{event_id}/report")
def get_report(ev: Event = Depends(admin_event), db: Session = Depends(get_db)):
    """최신 정산 보고서. 빠진 기준표가 있거나 아직 보고서가 없으면 지금 만든다."""
    rep = rpt.latest(db, ev)
    if rep is None or rpt.ensure_checkpoints(db, ev):
        rep = rpt.refresh(db, ev, "report_view")
        db.commit()
    versions = [{"version": r.version, "trigger": r.trigger, "created_at": clock.fmt(r.created_at), "stage": r.stage} for r in db.scalars(select(rpt.SettlementReport).where(rpt.SettlementReport.event_id == ev.id).order_by(rpt.SettlementReport.version.desc()).limit(30))]
    return rpt.report_out(rep) | {"versions": versions, "event": event_out(ev)}


@router.post("/events/{event_id}/report/refresh")
def refresh_report(ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    rep = rpt.refresh(db, ev, f"manual:{_actor(admin)}")
    db.commit()
    return rpt.report_out(rep)


@router.get("/events/{event_id}/report.csv")
def report_csv(ev: Event = Depends(admin_event), db: Session = Depends(get_db)):
    rep = rpt.latest(db, ev) or rpt.refresh(db, ev, "report_csv")
    db.commit()
    return RawResponse(
        content="\ufeff" + rpt.to_csv(rep), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="settlement_report_v{rep.version}.csv"'},
    )


@router.get("/events/{event_id}/agent/chat")
def admin_chat(ev: Event = Depends(admin_event), db: Session = Depends(get_db)):
    out = admin_agent.thread_for(db, ev)
    db.commit()
    return out


@router.post("/events/{event_id}/agent/chat")
def admin_chat_send(body: ChatIn, ev: Event = Depends(admin_event), db: Session = Depends(get_db)):
    out = admin_agent.handle_message(db, ev, body.text)
    db.commit()
    return {"thread": out["chat"], "reply": out["reply"]}


# ---------------------------------------------------------------- 알림(Slack DM)

@router.post("/events/{event_id}/emails/enqueue")
def enqueue(body: EmailEnqueue, ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    fns = {
        "invite": emails.enqueue_invites, "attend_reminder": emails.enqueue_attend_reminders, "payment_reminder": emails.enqueue_payment_reminders,
        "refund_notice": lambda d, e: emails.enqueue_settlement_notices(d, e, "refund_notice"),
        "extra_notice": lambda d, e: emails.enqueue_settlement_notices(d, e, "extra_notice"),
    }
    result = fns[body.kind](db, ev)
    audit(db, ev.id, _actor(admin), "emails_enqueued", detail={"kind": body.kind, **result})
    db.commit()
    return result


@router.post("/events/{event_id}/emails/dispatch")
def dispatch(ev: Event = Depends(admin_event), db: Session = Depends(get_db)):
    result = emails.dispatch_due(db, ev)
    db.commit()
    return result


@router.get("/events/{event_id}/emails/scheduler")
def scheduler_status(ev: Event = Depends(admin_event)):
    from ..services.scheduler import scheduler

    return scheduler.status()


@router.post("/events/{event_id}/emails/scheduler")
def scheduler_toggle(body: SchedulerToggle, ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    """이 서버 프로세스의 스케줄러를 일시정지·재개. 시작 자체는 NOTIFY_SCHEDULER 설정으로 정한다."""
    from ..services.scheduler import scheduler

    if body.enabled and not scheduler.running:
        raise conflict("SCHEDULER_NOT_STARTED", "스케줄러가 꺼진 설정으로 시작됐습니다. NOTIFY_SCHEDULER=on 으로 서버를 다시 시작하세요")
    scheduler.paused = not body.enabled
    audit(db, ev.id, _actor(admin), "email_scheduler_" + ("resumed" if body.enabled else "paused"))
    db.commit()
    return scheduler.status()


@router.get("/events/{event_id}/emails")
def list_emails(status: str | None = None, kind: str | None = None, ev: Event = Depends(admin_event), db: Session = Depends(get_db)):
    q = select(EmailJob).where(EmailJob.event_id == ev.id)
    if status:
        q = q.where(EmailJob.status == status)
    if kind:
        q = q.where(EmailJob.kind == kind)
    return [
        {"id": j.id, "kind": j.kind, "to_email": j.to_email, "subject": j.subject, "status": j.status, "attempts": j.attempts, "scheduled_at": clock.fmt(j.scheduled_at), "sent_at": clock.fmt(j.sent_at), "last_error": j.last_error, "note": j.note}
        for j in db.scalars(q.order_by(EmailJob.id.desc()).limit(400))
    ]


@router.get("/events/{event_id}/audit")
def audit_log(limit: int = Query(100, le=500), ev: Event = Depends(admin_event), db: Session = Depends(get_db)):
    return [
        {"id": a.id, "actor": a.actor, "action": a.action, "target": a.target, "detail": a.detail, "created_at": clock.fmt(a.created_at)}
        for a in db.scalars(select(AuditLog).where(AuditLog.event_id == ev.id).order_by(AuditLog.id.desc()).limit(limit))
    ]
