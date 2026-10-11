"""팀별 정산안 계산·확정·환불 송금 기록."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import clock
from ..errors import conflict, invalid
from ..models import CHOICE_LABELS, Admin, Allocation, BankTransaction, ChangeLog, ConfirmTask, Event, Participant, Payout, Settlement, Team
from . import fees
from .common import audit, get_team_in_event
from .payments import team_received
from .responses import fee_table

RULES_USED = [
    "회비: 버스 {bus:,}원 / 자차 {car:,}원 / 불참 {absent:,}원, 미정·미응답은 미확정",
    "마감 후 변경은 총무 승인분만 반영, 감액 변경은 환불 규칙의 환불률만큼만 환불",
    "수납액은 총무가 확정한 배정만 포함(개인 직접 입금도 소속 팀 수납액에 포함)",
    "정산 잔액 = 최종 회비 - 확정 수납액 + 이미 지급한 환불액 (양수 추가 납부, 음수 추가 환불)",
]


def refunds_paid(db: Session, event_id: int, team_id: int) -> int:
    return db.scalar(select(func.coalesce(func.sum(Payout.amount), 0)).where(Payout.event_id == event_id, Payout.team_id == team_id))


def compute_team(db: Session, event: Event, team: Team) -> dict:
    """저장하지 않고 정산안을 계산한다."""
    members = list(db.scalars(select(Participant).where(Participant.team_id == team.id).order_by(Participant.employee_no)))
    table = fee_table(db, event, team.id)
    changes: dict[int, list[ChangeLog]] = {}
    for c in db.scalars(select(ChangeLog).where(ChangeLog.participant_id.in_([m.id for m in members]), ChangeLog.after_deadline.is_(True)).order_by(ChangeLog.id)):
        changes.setdefault(c.participant_id, []).append(c)
    lines = []
    for m in members:
        st = table[m.id]
        lines.append(
            {
                "participant_id": m.id,
                "name": m.name,
                "snapshot_choice": st.snapshot_choice,
                "choice": st.choice,
                "choice_label": CHOICE_LABELS[st.choice],
                "fee": st.liability,
                "unresolved": st.unresolved,
                "changes": [
                    {
                        "change_id": c.id,
                        "from": c.from_choice,
                        "to": c.to_choice,
                        "status": c.status,
                        "refund_amount": c.refund_amount,
                        "extra_charge": c.extra_charge,
                        "rule": c.rule_text,
                    }
                    for c in changes.get(m.id, [])
                ],
            }
        )
    receipts = []
    q = (
        select(Allocation, BankTransaction)
        .join(BankTransaction, BankTransaction.id == Allocation.transaction_id)
        .where(Allocation.team_id == team.id, Allocation.status != "cancelled")
        .order_by(Allocation.id)
    )
    for a, t in db.execute(q):
        receipts.append(
            {
                "allocation_id": a.id,
                "transaction_id": t.id,
                "txn_at": clock.fmt(t.txn_at),
                "depositor": t.depositor,
                "amount": a.amount,
                "participant_id": a.participant_id,
                "kind": "개인" if a.participant_id else "팀",
                "status": a.status,
                "basis": a.basis,
            }
        )
    final_fee = sum(l["fee"] or 0 for l in lines)
    received = team_received(db, event.id, team.id)
    paid = refunds_paid(db, event.id, team.id)
    balance = fees.settlement_balance(final_fee, received, paid)

    blockers = []
    unresolved = [l["name"] for l in lines if l["unresolved"]]
    if unresolved:
        blockers.append(f"응답 미확정 {len(unresolved)}명: {', '.join(unresolved[:5])}{' 외' if len(unresolved) > 5 else ''}")
    pending = sum(1 for l in lines for c in l["changes"] if c["status"] == "pending_approval")
    if pending:
        blockers.append(f"총무 승인 대기 변경 {pending}건")
    proposed = sum(1 for r in receipts if r["status"] == "proposed")
    if proposed:
        blockers.append(f"확정 전 입금 배정 제안 {proposed}건")
    open_tasks = db.scalar(
        select(func.count()).select_from(ConfirmTask).where(
            ConfirmTask.team_id == team.id, ConfirmTask.blocking.is_(True), ConfirmTask.status.in_(("open", "answered", "escalated"))
        )
    )
    if open_tasks:
        blockers.append(f"해결 안 된 확인 과제 {open_tasks}건")
    return {
        "team_id": team.id,
        "team_code": team.code,
        "team_name": team.name,
        "headcount": len(lines),
        "final_fee": final_fee,
        "received": received,
        "refunds_paid": paid,
        "balance": balance,
        "balance_label": fees.balance_label(balance),
        "lines": lines,
        "receipts": receipts,
        "blockers": blockers,
        "rules": [RULES_USED[0].format(bus=event.fee_bus, car=event.fee_car, absent=event.fee_absent), *RULES_USED[1:]],
    }


def latest(db: Session, team_id: int) -> Settlement | None:
    return db.scalar(select(Settlement).where(Settlement.team_id == team_id, Settlement.status != "superseded").order_by(Settlement.version.desc()))


def save_draft(db: Session, event: Event, team: Team, now: datetime | None = None) -> Settlement:
    """정산안을 새 버전 draft 로 저장. 확정본은 그대로 두고, 이전 draft 는 superseded."""
    now = now or clock.now()
    calc = compute_team(db, event, team)
    prev_version = db.scalar(select(func.coalesce(func.max(Settlement.version), 0)).where(Settlement.team_id == team.id))
    for s in db.scalars(select(Settlement).where(Settlement.team_id == team.id, Settlement.status == "draft")):
        s.status = "superseded"
    s = Settlement(
        event_id=event.id,
        team_id=team.id,
        version=prev_version + 1,
        status="draft",
        final_fee=calc["final_fee"],
        received=calc["received"],
        refunds_paid=calc["refunds_paid"],
        balance=calc["balance"],
        detail={"lines": calc["lines"], "receipts": calc["receipts"], "rules": calc["rules"]},
        blockers=calc["blockers"],
        computed_at=now,
    )
    db.add(s)
    db.flush()
    return s


def confirm(db: Session, event: Event, team_id: int, admin: Admin, now: datetime | None = None) -> Settlement:
    team = get_team_in_event(db, event.id, team_id)
    draft = save_draft(db, event, team, now)
    if draft.blockers:
        raise conflict("SETTLEMENT_BLOCKED", "확정 전에 해결할 항목이 있습니다", draft.blockers)
    for s in db.scalars(select(Settlement).where(Settlement.team_id == team.id, Settlement.status == "confirmed")):
        s.status = "superseded"
    draft.status, draft.confirmed_at, draft.confirmed_by = "confirmed", now or clock.now(), admin.id
    audit(db, event.id, f"admin:{admin.id}", "settlement_confirmed", f"team:{team.id}", {"version": draft.version, "balance": draft.balance})
    return draft


def record_payout(db: Session, event: Event, team_id: int, amount: int, memo: str, admin: Admin, now: datetime | None = None) -> Payout:
    """총무가 앱 밖에서 송금한 환불 결과를 기록. 확정된 정산의 추가 환불액 이내에서만."""
    team = get_team_in_event(db, event.id, team_id)
    if amount <= 0:
        raise invalid("INVALID_AMOUNT", "송금액은 0보다 커야 합니다")
    s = db.scalar(select(Settlement).where(Settlement.team_id == team.id, Settlement.status == "confirmed"))
    if s is None:
        raise conflict("SETTLEMENT_NOT_CONFIRMED", "확정된 정산이 있어야 송금 결과를 기록할 수 있습니다")
    calc = compute_team(db, event, team)
    if calc["balance"] >= 0 or amount > -calc["balance"]:
        raise conflict("PAYOUT_EXCEEDS_REFUND", f"남은 환불액({max(0, -calc['balance']):,}원)을 넘는 송금은 기록할 수 없습니다")
    p = Payout(event_id=event.id, team_id=team.id, settlement_id=s.id, amount=amount, memo=memo, recorded_by=admin.id, recorded_at=now or clock.now())
    db.add(p)
    db.flush()
    audit(db, event.id, f"admin:{admin.id}", "payout_recorded", f"team:{team.id}", {"amount": amount})
    return p
