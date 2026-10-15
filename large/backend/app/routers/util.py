from __future__ import annotations

from typing import Any

from fastapi import Response as HttpResponse

from .. import clock
from ..config import API_PREFIX, settings
from ..models import CHOICE_LABELS, ChangeLog, ConfirmTask, Event
from ..security import COOKIE_NAMES


def set_session_cookie(resp: HttpResponse, role: str, token: str) -> None:
    resp.set_cookie(
        COOKIE_NAMES[role],
        token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path=API_PREFIX,
    )


def clear_session_cookie(resp: HttpResponse, role: str) -> None:
    resp.delete_cookie(COOKIE_NAMES[role], path=API_PREFIX)


def event_out(ev: Event) -> dict[str, Any]:
    return {
        "id": ev.id,
        "name": ev.name,
        "event_at": clock.fmt(ev.event_at),
        "attend_deadline": clock.fmt(ev.attend_deadline),
        "payment_deadline": clock.fmt(ev.payment_deadline),
        "fees": {"bus": ev.fee_bus, "car": ev.fee_car, "absent": ev.fee_absent},
        "account_note": ev.account_note,
        "snapshot_taken_at": clock.fmt(ev.snapshot_taken_at),
        "after_attend_deadline": clock.now() >= ev.attend_deadline,
        "bot": {"persona": ev.bot_persona or None, "strategy": ev.bot_strategy or None},
    }


def change_out(c: ChangeLog, name: str | None = None) -> dict[str, Any]:
    return {
        "id": c.id,
        "participant_id": c.participant_id,
        "participant_name": name,
        "from_choice": c.from_choice,
        "to_choice": c.to_choice,
        "from_label": CHOICE_LABELS[c.from_choice],
        "to_label": CHOICE_LABELS[c.to_choice],
        "after_deadline": c.after_deadline,
        "status": c.status,
        "source": c.source,
        "requested_at": clock.fmt(c.requested_at),
        "decided_at": clock.fmt(c.decided_at),
        "liability_before": c.liability_before,
        "liability_after": c.liability_after,
        "refund_amount": c.refund_amount,
        "extra_charge": c.extra_charge,
        "rule_text": c.rule_text,
        "rule_missing": c.rule_missing,
        "admin_refund_decided": c.admin_refund_decided,
        "evidence_message_id": c.evidence_message_id,
        "note": c.note,
    }


def task_out(t: ConfirmTask) -> dict[str, Any]:
    return {
        "id": t.id,
        "kind": t.kind,
        "assignee": t.assignee,
        "team_id": t.team_id,
        "participant_id": t.participant_id,
        "transaction_id": t.transaction_id,
        "change_id": t.change_id,
        "status": t.status,
        "blocking": t.blocking,
        "question": t.question,
        "options": t.options,
        "answer": t.answer,
        "answer_note": t.answer_note,
        "answered_at": clock.fmt(t.answered_at),
        "answer_message_id": t.answer_message_id,
        "resolution": t.resolution,
        "evidence": t.evidence,
        "rule": t.rule,
        "created_by": t.created_by,
        "created_at": clock.fmt(t.created_at),
        "updated_at": clock.fmt(t.updated_at),
    }
