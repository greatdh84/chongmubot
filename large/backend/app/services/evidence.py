"""'왜?'에 답하는 근거 모음(조회 전용, 저장하지 않음). 정산 보고서 근거 패널·총무 Agent get_evidence·대시보드가 같이 쓴다.

- team_evidence: 부서 하나의 청구(납부 마감 청구표) → 변동 줄(규칙·총무 결정·요청/승인 일시·경로) → 최종 회비,
  입금 내역(배정 상태·납부 마감 후 배지), 슬랙 대화, 확인 필요 사유(과제·승인 화면 링크)
- messages: 슬랙 근거 메시지(demo_slack) + 실제로 보낸 DM(EmailJob, source='dm_job')을 시간순으로 합친다
- timeline: Agent 활동 기록(감지 → 판단 → 실행 → 사람에게 넘김), 원래 시각 기준
- settlement_card: 정산 Agent 입금 대조 결과(자동 처리율, 판단마다 규칙/AI/사람 라벨과 근거)
금액은 settlement·report 코드가 계산한 값을 그대로 쓴다.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..models import (
    CHOICE_LABELS, AgentRun, Allocation, BankTransaction, ChangeLog, ConfirmTask, DevMail, EmailJob, Event, EvidenceMessage, Participant, Team, TeamBilling,
)
from .common import active_manager, won
from .report import SOURCE_LABEL, _basis, on_event_day
from .settlement import compute_team
from .slack import message_out

DM_KIND = {"invite": "인원조사 초대", "attend_reminder": "참석 독촉", "payment_reminder": "입금 독촉", "confirm_request": "확인 요청", "refund_notice": "환불 안내", "extra_notice": "추가 납부 안내", "manager_code": "인증코드"}
ACTIVE = ("open", "answered", "escalated")


def _kst(iso_or_dt: Any) -> str:
    dt = clock.parse_input(iso_or_dt) if isinstance(iso_or_dt, str) else iso_or_dt
    return clock.kst(dt).strftime("%m/%d %H:%M") if dt else "-"


# ---------------------------------------------------------------- 메시지(슬랙 + 보낸 DM)

def messages(db: Session, event: Event, team_id: int | None = None, participant_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
    names = {p.id: p.name for p in db.scalars(select(Participant).where(Participant.event_id == event.id))}
    q = select(EvidenceMessage).where(EvidenceMessage.event_id == event.id)
    out = []
    for m in db.scalars(q.order_by(EvidenceMessage.sent_at, EvidenceMessage.id)):
        if team_id is not None and m.team_id != team_id:
            continue
        if participant_id is not None and m.participant_id != participant_id and m.sender_participant_id != participant_id:
            continue
        out.append(message_out(m, names))
    jq = select(EmailJob).where(EmailJob.event_id == event.id, EmailJob.status == "sent")
    if team_id is not None:
        jq = jq.where(EmailJob.team_id == team_id)
    if participant_id is not None:
        jq = jq.where(EmailJob.participant_id == participant_id)
    jobs = list(db.scalars(jq.order_by(EmailJob.sent_at)))
    bodies = {d.email_job_id: d.body for d in db.scalars(select(DevMail).where(DevMail.email_job_id.in_([j.id for j in jobs])))} if jobs else {}
    for j in jobs:
        out.append({
            "id": f"dm{j.id}", "source": "dm_job", "sent_at": clock.fmt(j.sent_at), "sender": "총무봇", "recipient": names.get(j.participant_id, j.to_email),
            "team_id": j.team_id, "participant_id": j.participant_id, "participant_name": names.get(j.participant_id), "kind": j.kind, "kind_label": DM_KIND.get(j.kind, j.kind),
            "text": bodies.get(j.id) or j.subject, "subject": j.subject, "analysis": None,
        })
    out.sort(key=lambda x: (x["sent_at"] or "", str(x["id"])))
    return out[-limit:]


# ---------------------------------------------------------------- 부서 근거 패널

def team_evidence(db: Session, event: Event, team: Team) -> dict[str, Any]:
    calc = compute_team(db, event, team)
    billing = db.scalar(select(TeamBilling).where(TeamBilling.team_id == team.id))
    mgr = active_manager(db, team.id)
    mp = db.get(Participant, mgr.participant_id) if mgr else None
    names = {l["participant_id"]: l["name"] for l in calc["lines"]}
    deadline = event.payment_deadline

    # 1) 청구 → 최종
    bill = None
    if billing is not None:
        c = billing.counts
        parts = [{"label": "버스", "n": c.get("bus", 0), "fee": event.fee_bus}, {"label": "자차", "n": c.get("car", 0), "fee": event.fee_car}]
        if event.fee_absent:
            parts.append({"label": "불참", "n": c.get("absent", 0), "fee": event.fee_absent})
        bill = {
            "captured_at": clock.fmt(billing.captured_at), "deadline": clock.fmt(deadline), "parts": parts, "absent": c.get("absent", 0),
            "unresolved": billing.unresolved, "unresolved_names": [l["name"] for l in billing.lines if l["fee"] is None], "billed": billing.billed,
            "formula": " + ".join(f"{x['label']} {x['n']}×{x['fee']:,}" for x in parts if x["n"]) + f" = {billing.billed:,}원",
        }
    pids = list(names)
    msgs_by_change: dict[int, list[EvidenceMessage]] = {}
    for m in db.scalars(select(EvidenceMessage).where(EvidenceMessage.change_id.is_not(None), EvidenceMessage.event_id == event.id)):
        msgs_by_change.setdefault(m.change_id, []).append(m)
    lines, before_billing = [], []
    for ch in db.scalars(select(ChangeLog).where(ChangeLog.participant_id.in_(pids), ChangeLog.after_deadline.is_(True), ChangeLog.status != "rejected").order_by(ChangeLog.requested_at, ChangeLog.id)):
        kind, basis = _basis(ch)
        ev_msgs = [{"id": m.id, "sender": m.sender, "sent_at": clock.fmt(m.sent_at), "text": m.text, "kind": (m.analysis or {}).get("kind")} for m in msgs_by_change.get(ch.id, [])]
        item = {
            "change_id": ch.id, "participant_id": ch.participant_id, "name": names.get(ch.participant_id), "from": CHOICE_LABELS[ch.from_choice], "to": CHOICE_LABELS[ch.to_choice],
            "status": ch.status, "delta": (ch.extra_charge - ch.refund_amount) if ch.status == "applied" else None,
            "pending_delta": (ch.extra_charge - ch.refund_amount) if ch.status == "pending_approval" and not ch.rule_missing else None,
            "pending_amount_text": ("환불액 미정(규칙 없음 → 총무 판단)" if ch.rule_missing else f"승인되면 {'+' if ch.extra_charge else '−'}{(ch.extra_charge or ch.refund_amount):,}원") if ch.status == "pending_approval" else None,
            "basis_kind": kind, "basis": basis, "requested_at": clock.fmt(ch.requested_at), "decided_at": clock.fmt(ch.decided_at),
            "source": SOURCE_LABEL.get(ch.source, ch.source), "on_event_day": on_event_day(event, ch.requested_at), "note": ch.note, "messages": ev_msgs,
        }
        if billing is not None and ch.status == "applied" and (ch.decided_at or ch.requested_at) <= deadline:
            before_billing.append(item)
        else:
            lines.append(item)
    applied_delta = sum(x["delta"] or 0 for x in lines)
    calc_check = None if bill is None else {"billed": bill["billed"], "delta": applied_delta, "final": calc["final_fee"], "ok": bill["billed"] + applied_delta == calc["final_fee"]}

    # 2) 입금 내역(확정·제안 배정 + 이 부서에 걸린 입금 확인 과제)
    receipts = []
    by_deadline = 0
    alloc_rows = list(db.execute(
        select(Allocation, BankTransaction).join(BankTransaction, BankTransaction.id == Allocation.transaction_id)
        .where(Allocation.team_id == team.id, Allocation.status != "cancelled").order_by(BankTransaction.txn_at, Allocation.id)
    ))
    for a, t in alloc_rows:
        if a.status == "confirmed" and t.txn_at <= deadline:
            by_deadline += a.amount
    seen_txn = set()
    for a, t in alloc_rows:
        seen_txn.add(t.id)
        late = t.txn_at > deadline
        label = None
        if late:
            label = (f"추가 납부 완료 {_kst(t.txn_at)}" if a.status == "confirmed" else f"추가 납부(확정 전) {_kst(t.txn_at)}") if billing is not None and by_deadline >= billing.billed else "납부 마감 후 입금"
        receipts.append({
            "transaction_id": t.id, "allocation_id": a.id, "txn_at": clock.fmt(t.txn_at), "depositor": t.depositor, "amount": a.amount, "txn_amount": t.amount, "memo": t.memo,
            "status": a.status, "status_label": {"confirmed": "확정", "proposed": "제안"}[a.status], "confidence": a.confidence, "basis": a.basis,
            "participant_name": names.get(a.participant_id) if a.participant_id else None, "after_deadline": late, "late_label": label,
        })
    tasks = list(db.scalars(select(ConfirmTask).where(ConfirmTask.team_id == team.id, ConfirmTask.status.in_(ACTIVE)).order_by(ConfirmTask.id)))
    for task in tasks:
        if task.transaction_id and task.transaction_id not in seen_txn:
            t = db.get(BankTransaction, task.transaction_id)
            seen_txn.add(t.id)
            receipts.append({
                "transaction_id": t.id, "allocation_id": None, "txn_at": clock.fmt(t.txn_at), "depositor": t.depositor, "amount": t.amount, "txn_amount": t.amount, "memo": t.memo,
                "status": "task", "status_label": "과제", "confidence": "", "basis": task.question.split("\n")[0], "participant_name": None,
                "after_deadline": t.txn_at > deadline, "late_label": "납부 마감 후 입금" if t.txn_at > deadline else None, "task_id": task.id,
                "slack_suggestion": (task.evidence or {}).get("slack_suggestion"),
            })
    receipts.sort(key=lambda r: (r["txn_at"], r["allocation_id"] or 0))

    # 3) 슬랙 대화(+ 보낸 DM)
    msgs = messages(db, event, team_id=team.id)

    # 4) 확인 필요 사유(링크)
    blockers = []
    unresolved = [l["name"] for l in calc["lines"] if l["unresolved"]]
    if unresolved:
        blockers.append({"text": f"응답 미확정 {len(unresolved)}명: {', '.join(unresolved)}", "link": {"tab": "부서·명단"}})
    pend = [x for x in lines if x["status"] == "pending_approval"]
    if pend:
        blockers.append({"text": f"총무 승인 대기 변경 {len(pend)}건: " + ", ".join(f"{x['name']} {x['from']}→{x['to']}" for x in pend), "link": {"tab": "입금·확인", "sub": "변경 승인", "ids": [x["change_id"] for x in pend]}})
    undecided = [x for x in lines if x["status"] == "applied" and x["basis_kind"] == "pending"]
    if undecided:
        blockers.append({"text": f"환불 규칙 없이 임시 0원 {len(undecided)}건(총무 결정 필요)", "link": {"tab": "입금·확인", "sub": "변경 승인"}})
    prop = [r for r in receipts if r["status"] == "proposed"]
    if prop:
        blockers.append({"text": f"확정 전 입금 배정 제안 {len(prop)}건: " + ", ".join(f"{r['depositor']} {r['amount']:,}원" for r in prop), "link": {"tab": "입금·확인", "sub": "입금 대조", "ids": [r["transaction_id"] for r in prop]}})
    for task in tasks:
        if task.kind == "change_approval" or not task.blocking:
            continue
        sug = (task.evidence or {}).get("slack_suggestion")
        blockers.append({"text": f"확인 과제 #{task.id}: {task.question.splitlines()[0][:90]}", "task_id": task.id, "kind": task.kind,
                         "suggestion": f"슬랙 근거 → {sug['team']} 배정 후보" if sug else None, "link": {"tab": "입금·확인", "sub": "확인 과제", "ids": [task.id]}})

    return {
        "team": {"team_id": team.id, "code": team.code, "name": team.name, "headcount": calc["headcount"], "manager": mp.name if mp else None},
        "billing": bill, "before_billing": before_billing, "changes": lines, "final_fee": calc["final_fee"], "check": calc_check,
        "received": calc["received"], "refunds_paid": calc["refunds_paid"], "balance": calc["balance"],
        "receipts": receipts, "messages": msgs, "blockers": blockers,
    }


def person_evidence(db: Session, event: Event, p: Participant) -> dict[str, Any]:
    team = db.get(Team, p.team_id)
    te = team_evidence(db, event, team)
    return {
        "person": {"participant_id": p.id, "name": p.name, "team": team.name, "employee_no": p.employee_no},
        "team": te["team"], "billing": te["billing"], "final_fee": te["final_fee"], "balance": te["balance"],
        "changes": [x for x in te["before_billing"] + te["changes"] if x["participant_id"] == p.id],
        "messages": [m for m in te["messages"] if m.get("participant_id") == p.id or m.get("recipient") == p.name or m.get("sender") == p.name or f"{p.name}님" in (m.get("text") or "")],
        "blockers": te["blockers"],
    }


# ---------------------------------------------------------------- 정산 Agent 실행 카드(입금 대조)

def settlement_card(db: Session, event: Event) -> dict[str, Any]:
    txns = list(db.scalars(select(BankTransaction).where(BankTransaction.event_id == event.id).order_by(BankTransaction.txn_at, BankTransaction.id)))
    teams = {t.id: t for t in db.scalars(select(Team).where(Team.event_id == event.id))}
    allocs: dict[int, list[Allocation]] = {}
    for a in db.scalars(select(Allocation).where(Allocation.event_id == event.id, Allocation.status != "cancelled")):
        allocs.setdefault(a.transaction_id, []).append(a)
    tasks: dict[int, list[ConfirmTask]] = {}
    dup: dict[int, ConfirmTask] = {}
    for t in db.scalars(select(ConfirmTask).where(ConfirmTask.event_id == event.id, ConfirmTask.kind != "change_approval")):
        if t.transaction_id:
            tasks.setdefault(t.transaction_id, []).append(t)
        if t.kind == "possible_duplicate" and (t.evidence or {}).get("allocation_id"):
            dup[t.evidence["allocation_id"]] = t
    items, reasons = [], {}
    auto = 0
    for t in txns:
        al = allocs.get(t.id, [])
        ts = tasks.get(t.id, [])
        open_ts = [x for x in ts if x.status in ACTIVE]
        label, by, why, state = "", "rule", "", "done"
        d = next((dup[a.id] for a in al if a.id in dup), None)
        if al and all(a.status == "confirmed" for a in al) and all(a.source == "agent" and a.confidence == "high" for a in al) and not (d and d.status in ACTIVE):
            auto += 1
            label, why = "자동 처리", al[0].basis
        elif d and d.status in ACTIVE:
            label, state, why = "중복 의심", "human", d.question
        elif any(a.status == "proposed" for a in al):
            a = next(a for a in al if a.status == "proposed")
            label, state, why = "제안 확인", "human", a.basis
        elif open_ts and any(x.status == "escalated" for x in open_ts) and not any((x.evidence or {}).get("slack_suggestion") for x in open_ts):
            label, state, why = "총무 이관", "human", open_ts[0].question
        elif open_ts:
            sug = next(((x.evidence or {}).get("slack_suggestion") for x in open_ts if (x.evidence or {}).get("slack_suggestion")), None)
            label, state = "입금자 확인", "human"
            if sug:
                by = "ai" if str(sug.get("by", "")).startswith(("claude", "gemini")) else "rule"
                why = f"슬랙 근거 {sug['sender']} {_kst(sug['sent_at'])} “{sug['text']}” → {sug['team']} 후보 ({sug['why']})"
            else:
                why = open_ts[0].question.split("\n")[0]
        elif al:
            label, by = "사람 확인 완료", "human"
            why = "; ".join(a.basis for a in al)
        else:
            label, state, why = "미배정", "human", "배정·과제 없음(정산 Agent 점검 전)"
        if state == "human":
            reasons[label] = reasons.get(label, 0) + 1
        items.append({
            "transaction_id": t.id, "txn_at": clock.fmt(t.txn_at), "depositor": t.depositor, "amount": t.amount, "memo": t.memo,
            "teams": [teams[a.team_id].name for a in al], "label": label, "state": state, "by": by, "why": why,
            "task_ids": [x.id for x in open_ts] or ([d.id] if d and d.status in ACTIVE else []),
        })
    run = db.scalar(select(AgentRun).where(AgentRun.event_id == event.id, AgentRun.agent == "settlement", AgentRun.trigger.not_like("report:%"), AgentRun.trigger.not_like("%slack%")).order_by(AgentRun.id.desc()).limit(1))
    slack_run = db.scalar(select(AgentRun).where(AgentRun.event_id == event.id, AgentRun.agent == "settlement", AgentRun.trigger.like("%slack%"), AgentRun.trigger.not_like("report:%"), AgentRun.trigger != "slack_attach").order_by(AgentRun.id.desc()).limit(1))
    human = sum(reasons.values())
    return {
        "total": len(txns), "auto": auto, "human": human, "human_done": sum(1 for i in items if i["label"] == "사람 확인 완료"),
        "rate": round(auto * 100 / len(txns)) if txns else 0, "reasons": reasons, "items": items,
        "last_run": {"at": clock.fmt(run.created_at), "summary": run.result.get("summary"), "rules": run.rules} if run else None,
        "slack": {"at": clock.fmt(slack_run.created_at), "summary": slack_run.result.get("summary"), "by": slack_run.result.get("by"), "fallback": slack_run.result.get("fallback")} if slack_run else None,
    }


# ---------------------------------------------------------------- Agent 활동 기록(원래 시각 기준)

def _step(phase: str, text: str, by: str = "rule") -> dict[str, str]:
    return {"phase": phase, "text": text, "by": by}


def _by(a: dict) -> str:
    b = str(a.get("by") or "rule")
    return "ai" if b.startswith(("claude", "gemini")) else "fallback" if b == "mock_fallback" else "rule"


def timeline(db: Session, event: Event, limit: int = 120) -> list[dict[str, Any]]:
    """감지 → 판단 → 실행 → 사람에게 넘김. 시연 CSV 로 넣은 일도 CSV 의 원래 시각(응답·요청·승인·메시지·입금 시각)으로 놓는다."""
    names = {p.id: p for p in db.scalars(select(Participant).where(Participant.event_id == event.id))}
    teams = {t.id: t for t in db.scalars(select(Team).where(Team.event_id == event.id))}
    out: list[dict[str, Any]] = []

    if event.snapshot_taken_at:
        from .report import _deadline_counts

        c = _deadline_counts(db, event) or {}
        out.append({"at": clock.fmt(event.attend_deadline), "title": "참석 마감", "steps": [
            _step("감지", f"참석 응답 마감 {_kst(event.attend_deadline)}"),
            _step("판단", f"마감 기준: 버스 {c.get('bus', 0)} · 자차 {c.get('car', 0)} · 불참 {c.get('absent', 0)} · 미정 {c.get('undecided', 0)} · 미응답 {c.get('none', 0)}", "code"),
            _step("실행", f"사람별 마감 기준 응답 {sum(c.values())}명 저장(이후 변경은 승인으로만)", "code"),
            _step("넘김", f"미정·미응답 {c.get('undecided', 0) + c.get('none', 0)}명 → 담당자 확인(자동 불참 처리 안 함)", "human"),
        ]})
    billings = list(db.scalars(select(TeamBilling).where(TeamBilling.event_id == event.id)))
    if billings:
        reminders = [m for m in db.scalars(select(EvidenceMessage).where(EvidenceMessage.event_id == event.id, EvidenceMessage.sender == "총무봇").order_by(EvidenceMessage.sent_at)) if m.sent_at >= event.payment_deadline and "회비" in m.text]
        out.append({"at": clock.fmt(event.payment_deadline), "title": "납부 마감", "steps": [
            _step("감지", f"회비 납부 마감 {_kst(event.payment_deadline)}"),
            _step("판단", f"부서 {len(billings)}개 청구 합계 {sum(b.billed for b in billings):,}원 (청구 때 미확정 {sum(b.unresolved for b in billings)}명은 0원으로 따로 셈)", "code"),
            _step("실행", f"{_kst(event.payment_deadline.replace(minute=1))} 부서별 청구표 {len(billings)}개 확정(수정 금지)", "code"),
            _step("넘김", ("미납 부서 독촉: " + ", ".join(f"{teams[m.team_id].name if m.team_id else m.recipient} {_kst(m.sent_at)}" for m in reminders) + " (DM)") if reminders else "입금 대조는 거래내역 업로드 후 정산 Agent", "human"),
        ]})

    msgs_by_change: dict[int, EvidenceMessage] = {}
    for m in db.scalars(select(EvidenceMessage).where(EvidenceMessage.event_id == event.id).order_by(EvidenceMessage.sent_at, EvidenceMessage.id)):
        a = m.analysis or {}
        kind = a.get("kind")
        team = teams.get(m.team_id)
        who = f"{m.sender}({team.name})" if team and m.sender not in ("총무", "총무봇") else m.sender
        if kind == "bot_dm":
            out.append({"at": clock.fmt(m.sent_at), "title": f"총무봇 DM → {m.recipient}", "steps": [_step("실행", f"{m.recipient}님에게 DM: “{m.text}”", "code")], "message_id": m.id})
            continue
        if kind == "change":
            if a.get("change_id"):
                msgs_by_change.setdefault(a["change_id"], m)
            p = names.get(m.participant_id)
            steps = [_step("감지", f"슬랙 {who}: “{m.text}”"),
                     _step("판단", f"{p.name if p else '?'}님 {CHOICE_LABELS.get(a.get('from') or a.get('current'), '')}→{CHOICE_LABELS.get(a.get('to'), '')} · 확신도 {a.get('confidence')} · {a.get('reason', '')}", _by(a))]
            if a.get("outcome") == "proposed":
                steps += [_step("실행", f"변경 제안 #{a.get('change_id')} 생성 · {a.get('amount_text', '')} ({a.get('rule', '')})", "code"), _step("넘김", "총무 승인 대기(자동 확정 안 함)", "human")]
            elif a.get("outcome") == "linked":
                ch = db.get(ChangeLog, a["change_id"])
                amt = f"−{ch.refund_amount:,}원" if ch.refund_amount else f"+{ch.extra_charge:,}원" if ch.extra_charge else "금액 변동 없음"
                steps += [_step("실행", f"변경 기록 #{ch.id} 근거로 연결 · {amt} ({_basis(ch)[1]})", "code"),
                          _step("넘김", f"총무 승인 {_kst(ch.decided_at)}" if ch.status == "applied" else "총무 승인 대기", "human")]
            else:
                steps += [_step("실행", a.get("outcome_label") or "", "code")]
            out.append({"at": clock.fmt(m.sent_at), "title": f"참석 변경 감지 · {p.name if p else ''}", "steps": steps, "message_id": m.id, "change_id": a.get("change_id")})
        elif kind == "admin_refund":
            p = names.get(m.participant_id)
            out.append({"at": clock.fmt(m.sent_at), "title": f"총무 환불 결정 · {p.name if p else ''}", "message_id": m.id, "steps": [
                _step("감지", f"슬랙 총무 → {m.recipient}: “{m.text}”"), _step("판단", f"환불액 {a.get('suggested_refund') or ''} 총무 결정(규칙 없음)", _by(a)),
                _step("실행", a.get("outcome_label") or "", "code"), _step("넘김", "환불액은 총무가 승인 화면에서 확정", "human")]})
        elif kind == "deposit_hint":
            out.append({"at": clock.fmt(m.sent_at), "title": f"입금 단서 · {team.name if team else ''}", "message_id": m.id, "steps": [
                _step("감지", f"슬랙 {who}: “{m.text}”"), _step("판단", a.get("reason", ""), _by(a)), _step("실행", "입금자 확인 과제의 배정 후보로 대기(R7)", "code")]})
        elif kind in ("payment_promise",):
            out.append({"at": clock.fmt(m.sent_at), "title": f"납부 약속 · {team.name if team else ''}", "message_id": m.id, "steps": [_step("감지", f"슬랙 {who}: “{m.text}”"), _step("판단", "납부 약속(금액 변동 없음, 기록만)", _by(a))]})

    for ch in db.scalars(select(ChangeLog).where(ChangeLog.event_id == event.id, ChangeLog.after_deadline.is_(True)).order_by(ChangeLog.requested_at)):
        if ch.id in msgs_by_change:
            continue
        p = names.get(ch.participant_id)
        steps = [_step("감지", f"{SOURCE_LABEL.get(ch.source, ch.source)}: {p.name if p else ''}님 {CHOICE_LABELS[ch.from_choice]}→{CHOICE_LABELS[ch.to_choice]}" + (f" — {ch.note}" if ch.note else ""), "human" if ch.source == "admin" else "rule"),
                 _step("판단", _basis(ch)[1], "code")]
        steps.append(_step("넘김", f"총무 승인 {_kst(ch.decided_at)}" if ch.status == "applied" else "총무 승인 대기" if ch.status == "pending_approval" else "반려", "human"))
        out.append({"at": clock.fmt(ch.requested_at), "title": f"변경 · {p.name if p else ''}", "steps": steps, "change_id": ch.id})

    txns = list(db.scalars(select(BankTransaction).where(BankTransaction.event_id == event.id)))
    if txns:
        card = settlement_card(db, event)
        run = card["last_run"]
        out.append({"at": run["at"] if run else clock.fmt(max(t.txn_at for t in txns)), "title": "정산 Agent 입금 대조", "steps": [
            _step("감지", f"거래 {card['total']}건 읽음({_kst(min(t.txn_at for t in txns))} ~ {_kst(max(t.txn_at for t in txns))} 입금)"),
            _step("판단", (run or {}).get("summary") or "점검 전", "rule"),
            _step("실행", f"높은 확신 {card['auto']}건 확정(자동 처리율 {card['rate']}%)", "code"),
            _step("넘김", "사람 확인 " + (", ".join(f"{k} {v}" for k, v in card["reasons"].items()) or "없음"), "human"),
        ]})
    for j in db.scalars(select(EmailJob).where(EmailJob.event_id == event.id, EmailJob.status == "sent", EmailJob.kind.in_(("refund_notice", "extra_notice", "payment_reminder"))).order_by(EmailJob.sent_at)):
        p = names.get(j.participant_id)
        out.append({"at": clock.fmt(j.sent_at), "title": f"{DM_KIND.get(j.kind, j.kind)} DM", "steps": [_step("실행", f"{teams[j.team_id].name if j.team_id in teams else ''} 담당자 {p.name if p else j.to_email}에게 {DM_KIND.get(j.kind)} DM ({j.subject})", "code")]})
    out.sort(key=lambda x: x["at"] or "")
    return out[-limit:]
