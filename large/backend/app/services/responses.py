"""참석 응답·변경·마감 기준 응답(스냅샷)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..errors import conflict, invalid, not_found
from ..models import CHOICE_LABELS, CHOICES, Admin, ChangeLog, ConfirmTask, DeadlineSnapshot, Event, Participant, RefundRule, Response
from . import fees
from .common import audit, create_task, won


@dataclass
class FeeState:
    participant_id: int
    choice: Optional[str]
    snapshot_choice: Optional[str]
    liability: Optional[int]  # 현재 부담액(최종 회비). None = 미확정
    pending_change_id: Optional[int]

    @property
    def unresolved(self) -> bool:
        return self.liability is None


def after_attend_deadline(event: Event, now: datetime | None = None) -> bool:
    return (now or clock.now()) >= event.attend_deadline


def current_choice(db: Session, participant_id: int) -> Optional[str]:
    r = db.scalar(select(Response).where(Response.participant_id == participant_id))
    return r.choice if r else None


def ensure_snapshot(db: Session, event: Event, now: datetime | None = None) -> bool:
    """참석 마감이 지났고 아직 스냅샷이 없으면 현재 응답으로 마감 기준 응답을 저장. 이미 있으면 그대로 둔다."""
    now = now or clock.now()
    if event.snapshot_taken_at is not None or now < event.attend_deadline:
        return False
    responses = {r.participant_id: r.choice for r in db.scalars(select(Response).join(Participant).where(Participant.event_id == event.id))}
    for pid in db.scalars(select(Participant.id).where(Participant.event_id == event.id)):
        choice = responses.get(pid)
        db.add(DeadlineSnapshot(event_id=event.id, participant_id=pid, choice=choice, fee=fees.fee_for(event, choice), captured_at=now))
    event.snapshot_taken_at = now
    audit(db, event.id, "system", "snapshot_taken", detail={"count": len(responses)})
    db.flush()
    return True


def fee_table(db: Session, event: Event, team_id: int | None = None) -> dict[int, FeeState]:
    """참가자별 현재 부담액. 스냅샷 전에는 현재 선택의 회비, 후에는 스냅샷 + 승인된 변경의 부담액."""
    q = select(Participant.id).where(Participant.event_id == event.id)
    if team_id is not None:
        q = q.where(Participant.team_id == team_id)
    pids = list(db.scalars(q))
    choices = {r.participant_id: r.choice for r in db.scalars(select(Response).where(Response.participant_id.in_(pids)))}
    snaps = {s.participant_id: s for s in db.scalars(select(DeadlineSnapshot).where(DeadlineSnapshot.event_id == event.id, DeadlineSnapshot.participant_id.in_(pids)))}
    last_applied: dict[int, ChangeLog] = {}
    pending: dict[int, int] = {}
    for c in db.scalars(select(ChangeLog).where(ChangeLog.participant_id.in_(pids)).order_by(ChangeLog.id)):
        if c.status == "applied" and c.after_deadline:
            last_applied[c.participant_id] = c
        elif c.status == "pending_approval":
            pending[c.participant_id] = c.id
    out: dict[int, FeeState] = {}
    for pid in pids:
        choice = choices.get(pid)
        if event.snapshot_taken_at is None:
            liability = fees.fee_for(event, choice)
            snap_choice = None
        else:
            snap = snaps.get(pid)
            snap_choice = snap.choice if snap else None
            liability = last_applied[pid].liability_after if pid in last_applied else (snap.fee if snap else None)
        out[pid] = FeeState(pid, choice, snap_choice, liability, pending.get(pid))
    return out


def _rules(db: Session, event: Event) -> list[RefundRule]:
    return list(db.scalars(select(RefundRule).where(RefundRule.event_id == event.id)))


def preview_change(db: Session, event: Event, participant: Participant, to_choice: str, now: datetime | None = None) -> dict:
    """변경을 기록하지 않고 결과를 미리 계산. 소통 Agent 의 확인 질문과 화면 미리보기에 같이 쓴다."""
    now = now or clock.now()
    if to_choice not in CHOICES:
        raise invalid("INVALID_CHOICE", "선택지는 bus / car / absent / undecided 중 하나여야 합니다")
    from_choice = current_choice(db, participant.id)
    if from_choice == to_choice:
        raise conflict("NO_CHANGE", f"이미 '{CHOICE_LABELS[to_choice]}'(으)로 응답되어 있습니다")
    existing = db.scalar(select(ChangeLog).where(ChangeLog.participant_id == participant.id, ChangeLog.status == "pending_approval"))
    if existing is not None:
        raise conflict("PENDING_CHANGE_EXISTS", "총무 승인을 기다리는 변경 요청이 이미 있습니다", {"change_id": existing.id})
    base = {
        "participant_id": participant.id,
        "from_choice": from_choice,
        "to_choice": to_choice,
        "from_label": CHOICE_LABELS[from_choice],
        "to_label": CHOICE_LABELS[to_choice],
    }
    if not after_attend_deadline(event, now):
        return base | {
            "after_deadline": False,
            "requires_approval": False,
            "fee_before": fees.fee_for(event, from_choice),
            "fee_after": fees.fee_for(event, to_choice),
            "refund_amount": 0,
            "extra_charge": 0,
            "rule_text": "참석 마감 전 변경: 현재 선택의 회비가 그대로 적용",
        }
    if to_choice == "undecided":
        raise invalid("UNDECIDED_AFTER_DEADLINE", "참석 마감 후에는 '미정'으로 바꿀 수 없습니다")
    ensure_snapshot(db, event, now)
    state = fee_table(db, event, participant.team_id)[participant.id]
    ev = fees.evaluate_post_deadline_change(event, _rules(db, event), from_choice, to_choice, state.liability, now)
    return base | {
        "after_deadline": True,
        "requires_approval": True,
        "fee_before": ev.liability_before,
        "fee_after": ev.liability_after,
        "refund_amount": ev.refund_amount,
        "extra_charge": ev.extra_charge,
        "rule_id": ev.rule_id,
        "rule_text": ev.rule_text,
        "needs_admin_rule": ev.needs_admin_rule,
    }


def submit_change(
    db: Session,
    event: Event,
    participant: Participant,
    to_choice: str,
    *,
    source: str,
    actor: str,
    evidence_message_id: int | None = None,
    note: str = "",
    now: datetime | None = None,
) -> ChangeLog:
    """명시적 제출로만 호출. 마감 전이면 즉시 반영, 마감 후면 총무 승인 대기 + 확인 과제 생성."""
    now = now or clock.now()
    pv = preview_change(db, event, participant, to_choice, now)
    change = ChangeLog(
        event_id=event.id,
        participant_id=participant.id,
        from_choice=pv["from_choice"],
        to_choice=to_choice,
        after_deadline=pv["after_deadline"],
        source=source,
        requested_at=now,
        evidence_message_id=evidence_message_id,
        rule_text=pv["rule_text"],
        note=note,
        status="pending_approval" if pv["requires_approval"] else "applied",
    )
    if pv["after_deadline"]:
        change.liability_before = pv["fee_before"]
        change.liability_after = pv["fee_after"]
        change.refund_amount = pv["refund_amount"]
        change.extra_charge = pv["extra_charge"]
        change.refund_rule_id = pv.get("rule_id")
    else:
        change.decided_at = now
        _set_response(db, participant.id, to_choice, source, now)
    db.add(change)
    db.flush()
    if change.status == "pending_approval":
        amount_txt = (
            f"환불 {won(change.refund_amount)}" if change.refund_amount else (f"추가 {won(change.extra_charge)}" if change.extra_charge else "금액 변동 없음")
        )
        create_task(
            db,
            dedupe_key=f"change_approval:{change.id}",
            event_id=event.id,
            kind="change_approval",
            assignee="admin",
            team_id=participant.team_id,
            participant_id=participant.id,
            change_id=change.id,
            question=f"{participant.name}님 마감 후 변경 요청: {pv['from_label']} → {pv['to_label']} ({amount_txt}, 규칙: {change.rule_text}). 승인할까요?",
            options=[{"key": "approve", "label": "승인"}, {"key": "reject", "label": "반려"}],
            rule=change.rule_text,
            evidence={"preview": pv, "evidence_message_id": evidence_message_id},
            created_by="communication_agent" if source == "chat" else "system",
        )
    audit(db, event.id, actor, "change_submitted", f"change:{change.id}", {"status": change.status, "to": to_choice})
    return change


def _set_response(db: Session, participant_id: int, choice: str, source: str, now: datetime) -> None:
    r = db.scalar(select(Response).where(Response.participant_id == participant_id))
    if r is None:
        db.add(Response(participant_id=participant_id, choice=choice, source=source, updated_at=now))
    else:
        r.choice, r.source, r.updated_at = choice, source, now


def decide_change(db: Session, event: Event, change: ChangeLog, admin: Admin, approve: bool, note: str = "", now: datetime | None = None) -> ChangeLog:
    now = now or clock.now()
    if change.event_id != event.id:
        raise not_found("변경 요청")
    if change.status != "pending_approval":
        raise conflict("ALREADY_DECIDED", "이미 처리된 변경 요청입니다")
    if approve:
        # 승인 시점의 부담액으로 다시 계산하되, 환불 규칙은 '요청 시각' 기준으로 적용한다.
        participant = db.get(Participant, change.participant_id)
        state = fee_table(db, event, participant.team_id)[participant.id]
        ev = fees.evaluate_post_deadline_change(event, _rules(db, event), change.from_choice, change.to_choice, state.liability, change.requested_at)
        change.liability_before, change.liability_after = ev.liability_before, ev.liability_after
        change.refund_amount, change.extra_charge = ev.refund_amount, ev.extra_charge
        change.refund_rule_id, change.rule_text = ev.rule_id, ev.rule_text
        change.status = "applied"
        _set_response(db, change.participant_id, change.to_choice, "admin_approved", now)
    else:
        change.status = "rejected"
    change.decided_at, change.decided_by = now, admin.id
    if note:
        change.note = (change.note + " / " if change.note else "") + note
    task = db.scalar(select(ConfirmTask).where(ConfirmTask.dedupe_key == f"change_approval:{change.id}"))
    if task is not None:
        task.status, task.answer = "resolved", "approve" if approve else "reject"
        task.resolution = f"총무가 {'승인' if approve else '반려'}함" + (f": {note}" if note else "")
        task.updated_at = now
    audit(db, event.id, f"admin:{admin.id}", "change_approved" if approve else "change_rejected", f"change:{change.id}")
    return change
