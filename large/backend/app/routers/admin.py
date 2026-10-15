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
from ..schemas import AdminResponseSet, AllocationIn, BotSettings, ChatSend as ChatIn, Decision, EmailEnqueue, EventUpdate, ManagerAssign, PayoutIn, RefundRuleIn, RevealIn, SchedulerToggle, TaskResolve
from ..services import demo_import, emails, evidence as evid, invites, payments, refunds, report as rpt, responses as resp_svc, roster, settlement as stl, slack as slack_svc
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


@router.post("/events/{event_id}/demo/slack")
async def demo_slack(file: UploadFile = File(...), ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    """시연용 슬랙 대화 CSV → 근거 메시지. 정산 Agent 가 읽어 변경 제안·입금자 근거를 만든다(확정은 총무)."""
    if not settings.is_demo:
        raise not_found("API")
    out = slack_svc.import_csv(db, ev, _actor(admin), await _read_upload(file))
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
    delta: dict[int, int] = {}
    for c in db.scalars(select(ChangeLog).where(ChangeLog.event_id == ev.id, ChangeLog.after_deadline.is_(True), ChangeLog.status == "applied")):
        delta[c.participant_id] = delta.get(c.participant_id, 0) + c.extra_charge - c.refund_amount
    out = []
    for p in db.scalars(query.order_by(Participant.employee_no).limit(400)):
        st = table.get(p.id)
        out.append(
            {
                "id": p.id, "employee_no": p.employee_no, "name": p.name, "slack_id": p.slack_id, "phone": p.phone,
                "team": {"id": p.team_id, "code": teams[p.team_id].code, "name": teams[p.team_id].name},
                "is_manager": p.id in managers, "choice": st.choice if st else None, "choice_label": CHOICE_LABELS[st.choice if st else None],
                "snapshot_choice": st.snapshot_choice if st else None, "fee": st.liability if st else None,
                "pending_change_id": st.pending_change_id if st else None, "invite": invites.status(db, p.id), "delta": delta.get(p.id),
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
    rows = list(db.execute(q.order_by(ChangeLog.id.desc()).limit(300)))
    msgs = slack_svc.messages_by_change(db, [c.id for c, _ in rows])
    out = []
    for c, name in rows:
        o = change_out(c, name)
        ms = msgs.get(c.id, [])
        o["evidence"] = [slack_svc.message_out(m) for m in ms]
        sug = next(((m.analysis or {}).get("suggested_refund") for m in ms if (m.analysis or {}).get("outcome") == "suggest_refund"), None)
        if sug is not None:
            m = next(m for m in ms if (m.analysis or {}).get("outcome") == "suggest_refund")
            o["suggested_refund"], o["suggested_note"] = sug, f"총무 슬랙 결정({clock.kst(m.sent_at):%m/%d %H:%M}): {m.text[:80]}"
        out.append(o)
    return out


def _get_change(db: Session, ev: Event, change_id: int) -> ChangeLog:
    c = db.get(ChangeLog, change_id)
    if c is None or c.event_id != ev.id:
        raise not_found("변경 요청")
    return c


@router.post("/events/{event_id}/changes/{change_id}/approve")
def approve_change(change_id: int, body: Decision, ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    c = resp_svc.decide_change(db, ev, _get_change(db, ev, change_id), admin, approve=True, note=body.note, refund_amount=body.refund_amount)
    _refresh(db, ev, "change_approved")
    p = db.get(Participant, c.participant_id)
    amt = f"환불 {c.refund_amount:,}원" if c.refund_amount else f"추가 {c.extra_charge:,}원" if c.extra_charge else "금액 변동 없음"
    report = admin_agent.report_action(db, ev, kind="change_approved", team_id=p.team_id, what=f"{p.name}님 {CHOICE_LABELS[c.from_choice]}→{CHOICE_LABELS[c.to_choice]} 승인했어요({amt}{', 총무 결정' if c.admin_refund_decided else ''}).")
    db.commit()
    return change_out(c) | {"agent_report": report}


@router.post("/events/{event_id}/changes/{change_id}/reject")
def reject_change(change_id: int, body: Decision, ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    c = resp_svc.decide_change(db, ev, _get_change(db, ev, change_id), admin, approve=False, note=body.note)
    _refresh(db, ev, "change_rejected")
    p = db.get(Participant, c.participant_id)
    report = admin_agent.report_action(db, ev, kind="change_rejected", team_id=p.team_id, what=f"{p.name}님 {CHOICE_LABELS[c.from_choice]}→{CHOICE_LABELS[c.to_choice]} 변경을 반려했어요.")
    db.commit()
    return change_out(c) | {"agent_report": report}


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
    txn = db.get(BankTransaction, a.transaction_id)
    report = admin_agent.report_action(db, ev, kind="allocation_confirmed", team_id=a.team_id, what=f"{txn.depositor} {a.amount:,}원 입금을 확정했어요.")
    db.commit()
    return {"id": a.id, "status": a.status, "agent_report": report}


@router.post("/events/{event_id}/allocations/confirm-high")
def confirm_high(ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    """신뢰도 high(팀코드 + 팀원 명의) 제안만 한 번에 확정. 나머지는 하나씩 검토."""
    ids = list(db.scalars(select(Allocation.id).where(Allocation.event_id == ev.id, Allocation.status == "proposed", Allocation.confidence == "high")))
    for aid in ids:
        payments.confirm_allocation(db, ev, aid, admin.id)
    _refresh(db, ev, "allocations_confirmed_high")
    report = admin_agent.report_action(db, ev, kind="allocations_confirmed_high", team_id=None, what=f"높은 확신(팀코드 + 팀원 명의) 입금 {len(ids)}건을 한 번에 확정했어요.")
    db.commit()
    return {"confirmed": len(ids), "agent_report": report}


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
    result["agent_report"] = admin_agent.report_action(db, ev, kind="settlement_review", team_id=None, what=f"입금 대조를 마쳤어요: {result['summary']}.")
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
    report = admin_agent.report_action(db, ev, kind="task_resolved", team_id=t.team_id, what=f"확인 과제 #{t.id}를 처리했어요({body.resolution[:40]}).")
    db.commit()
    return task_out(t) | {"agent_report": report}


@router.post("/events/{event_id}/tasks/{task_id}/accept-suggestion")
def accept_suggestion(task_id: int, ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    """입금자 확인 과제: 정산 Agent 가 붙인 슬랙 근거의 배정 후보를 총무가 보고 지정 → 확정 배정 + 그 거래의 과제 닫기."""
    t = db.get(ConfirmTask, task_id)
    if t is None or t.event_id != ev.id:
        raise not_found("확인 과제")
    a = settlement_agent.accept_slack_suggestion(db, ev, t, admin.id)
    audit(db, ev.id, _actor(admin), "slack_suggestion_accepted", f"task:{t.id}", {"allocation_id": a.id})
    _refresh(db, ev, "slack_suggestion_accepted")
    txn = db.get(BankTransaction, a.transaction_id)
    report = admin_agent.report_action(db, ev, kind="slack_suggestion_accepted", team_id=a.team_id, what=f"'{txn.depositor}' {a.amount:,}원을 슬랙 근거대로 {db.get(Team, a.team_id).name} 입금으로 지정했어요.")
    db.commit()
    return {"allocation_id": a.id, "team_id": a.team_id, "amount": a.amount, "agent_report": report}


@router.get("/events/{event_id}/agent/briefing")
def agent_briefing(ev: Event = Depends(admin_event), db: Session = Depends(get_db)):
    """대시보드를 열면 Agent 가 먼저 하는 보고(조회만, 상태 안 바꿈). 본 시각은 POST /agent/briefing/seen 으로 남긴다."""
    from ..agents import admin_cards

    rep = rpt.latest(db, ev)
    if rep is None or rpt.ensure_checkpoints(db, ev):
        rep = rpt.refresh(db, ev, "report_view")
        db.commit()
    return admin_cards.briefing(db, ev, rep)


@router.post("/events/{event_id}/agent/briefing/seen")
def agent_briefing_seen(ev: Event = Depends(admin_event), db: Session = Depends(get_db)):
    from ..agents import admin_cards

    admin_cards.mark_seen(db, ev)
    db.commit()
    return {"ok": True}


@router.get("/events/{event_id}/agent/timeline")
def agent_timeline(ev: Event = Depends(admin_event), db: Session = Depends(get_db)):
    return evid.timeline(db, ev)


@router.get("/events/{event_id}/agent/settlement-card")
def agent_settlement_card(ev: Event = Depends(admin_event), db: Session = Depends(get_db)):
    return evid.settlement_card(db, ev)


@router.get("/events/{event_id}/evidence/messages")
def evidence_messages(team_id: int | None = None, participant_id: int | None = None, ev: Event = Depends(admin_event), db: Session = Depends(get_db)):
    """근거 메시지: 슬랙 대화(demo_slack) + 실제로 보낸 DM(dm_job)을 시간순으로."""
    if team_id is not None:
        get_team_in_event(db, ev.id, team_id)
    if participant_id is not None:
        get_participant_in_event(db, ev.id, participant_id)
    return evid.messages(db, ev, team_id=team_id, participant_id=participant_id)


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
def get_report(since: int | None = None, ev: Event = Depends(admin_event), db: Session = Depends(get_db)):
    """최신 정산 보고서. 빠진 기준표가 있거나 아직 보고서가 없으면 지금 만든다.
    since=N 이면 diff 를 바로 앞 버전 대신 vN 과 비교해 준다(예: 총무가 마지막으로 본 버전 이후 바뀐 부서)."""
    rep = rpt.latest(db, ev)
    if rep is None or rpt.ensure_checkpoints(db, ev):
        rep = rpt.refresh(db, ev, "report_view")
        db.commit()
    versions = [{"version": r.version, "trigger": r.trigger, "created_at": clock.fmt(r.created_at), "stage": r.stage} for r in db.scalars(select(rpt.SettlementReport).where(rpt.SettlementReport.event_id == ev.id).order_by(rpt.SettlementReport.version.desc()).limit(30))]
    out = rpt.report_out(rep) | {"versions": versions, "event": event_out(ev), "diff_base": rep.version - 1}
    if since is not None and since < rep.version:
        base = db.scalar(select(rpt.SettlementReport).where(rpt.SettlementReport.event_id == ev.id, rpt.SettlementReport.version == since))
        if base is not None:
            out["diff"] = rpt._diff(base, rep.teams, rep.people)
            out["diff_base"] = since
    return out


@router.get("/events/{event_id}/report/teams/{team_id}/evidence")
def report_team_evidence(team_id: int, ev: Event = Depends(admin_event), db: Session = Depends(get_db)):
    """부서 근거 패널: 청구 → 변동 → 최종, 입금 내역, 슬랙 대화, 확인 필요 사유."""
    return evid.team_evidence(db, ev, get_team_in_event(db, ev.id, team_id))


@router.post("/events/{event_id}/report/refresh")
def refresh_report(ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    rep = rpt.refresh(db, ev, f"manual:{_actor(admin)}")
    db.commit()
    return rpt.report_out(rep)


def _csv(content: str, filename: str) -> RawResponse:
    # UTF-8 BOM: 엑셀에서 한글이 깨지지 않게
    return RawResponse(content="\ufeff" + content, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/events/{event_id}/report.csv")
def report_csv(kind: str | None = None, ev: Event = Depends(admin_event), db: Session = Depends(get_db)):
    """정산 결과 내보내기. kind 없음 = 보고서 전체(부서 + 사람), teams = 부서별 정산표, people = 사람별 변동표."""
    rep = rpt.latest(db, ev) or rpt.refresh(db, ev, "report_csv")
    db.commit()
    if kind == "teams":
        return _csv(rpt.teams_csv(rep), f"settlement_teams_v{rep.version}.csv")
    if kind == "people":
        return _csv(rpt.people_csv(rep), f"settlement_people_v{rep.version}.csv")
    return _csv(rpt.to_csv(rep), f"settlement_report_v{rep.version}.csv")


@router.get("/events/{event_id}/workbench")
def workbench(ev: Event = Depends(admin_event), db: Session = Depends(get_db)):
    """대시보드 작업대: 급한 순 부서 목록(할 일 n) + 부서 밖 과제 + 첫 선택 부서(브리핑의 가장 급한 것)."""
    from ..agents import admin_cards

    rep = rpt.latest(db, ev)
    if rep is None or rpt.ensure_checkpoints(db, ev):
        rep = rpt.refresh(db, ev, "report_view")
        db.commit()
    out = evid.workbench(db, ev, rep)
    urgent = admin_cards.briefing(db, ev, rep, mark_new=False)["urgent"]
    first = (urgent or {}).get("team_id") or next((t["team_id"] for t in out["teams"] if t["todo"]), None) or (out["teams"][0]["team_id"] if out["teams"] else None)
    return out | {"first_team_id": first, "urgent": urgent}


# ---------------------------------------------------------------- 환불 계좌·송금 (실제 송금은 하지 않음 — 총무가 은행 앱에서 보낸 뒤 표시만)

@router.get("/events/{event_id}/refunds")
def refund_rows(ev: Event = Depends(admin_event), db: Session = Depends(get_db)):
    return {"rows": refunds.rows(db, ev), "demo_note": refunds.DEMO_NOTE if settings.is_demo else ""}


@router.post("/events/{event_id}/refunds/{team_id}/account/{account_id}/confirm")
def refund_account_confirm(team_id: int, account_id: int, ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    """슬랙 답장에서 읽은 계좌 후보를 총무가 확인해 저장."""
    team = get_team_in_event(db, ev.id, team_id)
    a = refunds.confirm_candidate(db, ev, team, account_id, admin)
    report = admin_agent.report_action(db, ev, kind="refund_account_confirmed", team_id=team.id, what=f"{team.name} 환불 계좌({a.bank} {refunds.mask(a.account_no)})를 저장했어요. 은행 앱에서 보낸 뒤 '송금 완료'를 눌러 주세요.")
    db.commit()
    return {"account": refunds.account_out(a), "agent_report": report}


@router.post("/events/{event_id}/refunds/{team_id}/account/reveal")
def refund_account_reveal(team_id: int, body: RevealIn, ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    """총무만: 전체 계좌번호(보기·복사). 꺼낸 사실은 감사 로그에 남는다(번호 없이)."""
    out = refunds.reveal(db, ev, get_team_in_event(db, ev.id, team_id), admin, body.purpose)
    db.commit()
    return out


@router.delete("/events/{event_id}/refunds/{team_id}/account")
def refund_account_delete(team_id: int, ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    n = refunds.remove(db, ev, get_team_in_event(db, ev.id, team_id), admin)
    db.commit()
    return {"deleted": n}


@router.post("/events/{event_id}/refunds/{team_id}/paid")
def refund_paid(team_id: int, ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    """총무가 은행 앱에서 보낸 뒤 '송금 완료 표시' → 정산 확정 + 환불 지급 기록 → 잔액 0 → 완료."""
    team = get_team_in_event(db, ev.id, team_id)
    out = refunds.mark_paid(db, ev, team, admin)
    _refresh(db, ev, "payout_recorded")
    report = admin_agent.report_action(db, ev, kind="refund_paid", team_id=team.id, what=f"{team.name} 환불 {out['amount']:,}원 송금 완료로 표시했어요.")
    db.commit()
    return out | {"agent_report": report}


@router.get("/events/{event_id}/refunds/payouts.csv")
def payout_sheet(confirm: str = "", ev: Event = Depends(admin_event), admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    """송금 명세 CSV — 전체 계좌번호가 들어가는 유일한 내보내기. 화면에서 한 번 더 확인한 뒤 confirm=yes 로만."""
    if confirm != "yes":
        raise invalid("CONFIRM_REQUIRED", "전체 계좌번호가 들어가는 파일입니다. 확인 후 다시 요청하세요(confirm=yes)")
    content = refunds.payout_sheet_csv(db, ev, admin)
    db.commit()
    return _csv(content, "refund_payouts.csv")


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
        "refund_notice": lambda d, e: emails.enqueue_settlement_notices(d, e, "refund_notice", body.team_ids),
        "extra_notice": lambda d, e: emails.enqueue_settlement_notices(d, e, "extra_notice", body.team_ids),
    }
    result = fns[body.kind](db, ev)
    audit(db, ev.id, _actor(admin), "emails_enqueued", detail={"kind": body.kind, **result})
    db.commit()
    return result


@router.post("/events/{event_id}/emails/preview")
def preview_notices(body: EmailEnqueue, ev: Event = Depends(admin_event), db: Session = Depends(get_db)):
    """담당자 안내 미리보기(보내지 않음): 부서·담당자·금액·대상 팀원·실제 문구."""
    if body.kind not in ("refund_notice", "extra_notice"):
        raise invalid("INVALID_KIND", "미리보기는 환불 안내·추가 납부 안내만 됩니다")
    return emails.preview_settlement_notices(db, ev, body.kind)


@router.get("/events/{event_id}/emails/sent")
def sent_notices(kind: str | None = None, ev: Event = Depends(admin_event), db: Session = Depends(get_db)):
    """보낸 메시지(모의 발송이면 개발용 수신함 본문까지)."""
    from ..models import DevMail

    q = select(EmailJob).where(EmailJob.event_id == ev.id, EmailJob.status == "sent")
    if kind:
        q = q.where(EmailJob.kind == kind)
    jobs = list(db.scalars(q.order_by(EmailJob.sent_at.desc(), EmailJob.id.desc()).limit(100)))
    bodies = {d.email_job_id: d.body for d in db.scalars(select(DevMail).where(DevMail.email_job_id.in_([j.id for j in jobs])))} if jobs else {}
    names = {p.id: p.name for p in db.scalars(select(Participant).where(Participant.event_id == ev.id))}
    teams = {t.id: t.name for t in db.scalars(select(Team).where(Team.event_id == ev.id))}
    return [
        {"id": j.id, "kind": j.kind, "team": teams.get(j.team_id), "to": names.get(j.participant_id), "subject": j.subject, "body": bodies.get(j.id), "sent_at": clock.fmt(j.sent_at), "mode": settings.email_mode}
        for j in jobs
    ]


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
