"""정산 보고서: 마감 때 저장한 '기준표'와 그 뒤 쌓인 '변동 기록'을 합쳐 한 장으로 만든다.

저장 구조(무엇이 언제 저장되고, 보고서는 무엇으로 만들어지나)
┌ 기준표(한 번 저장하면 고치지 않음) ───────────────────────────────────────────────┐
│ ① 참석 마감  → DeadlineSnapshot  : 참가자별 마감 기준 응답·회비                    │
│ ② 납부 마감  → TeamBilling       : 부서(팀)별 청구액 = 담당자가 걷어 낸 기준 금액    │
└─────────────────────────────────────────────────────────────────────────────────────┘
┌ 변동 기록(추가만 함) ───────────────────────────────────────────────────────────────┐
│ ChangeLog        : 마감 후 변경·행사 당일 변동(누가, 언제, 무엇에서 무엇으로, 적용 규칙) │
│ BankTransaction + Allocation : 은행 입금과 그 입금이 어느 팀 돈인지(Agent 제안 → 총무 확정)│
│ Payout           : 총무가 담당자에게 실제로 돌려준 환불                                │
└─────────────────────────────────────────────────────────────────────────────────────┘
→ SettlementReport : 정산 Agent 가 위 둘을 합쳐 다시 계산할 때마다 버전으로 저장(내용이 같으면 새 버전 없음)
   부서별: 청구액 · 최종 회비 · 확정 입금 · 지급 환불 → 정산 잔액(+ 추가 납부 / − 환불) · 확인 필요
   사람별: 청구 때 회비와 최종 회비가 다른 사람(왜 달라졌는지 근거 포함), 미확정·승인 대기
   이전 버전과 달라진 부서·사람(diff)

금액은 모두 코드(fees·settlement)가 계산한다. LLM 은 이 보고서를 읽어 답할 뿐 숫자를 만들지 않는다.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import clock
from ..models import CHOICE_LABELS, AgentRun, Allocation, BankTransaction, ChangeLog, Event, Participant, SettlementReport, Team, TeamBilling
from . import responses
from .common import active_manager, audit
from .settlement import compute_team

STAGES = {
    "collecting": "인원조사 중",
    "attend_closed": "참석 마감 → 회비 납부 중",
    "payment_closed": "납부 마감 → 행사 전",
    "event_done": "행사 종료 → 정산",
}
SOURCE_LABEL = {"web": "참가자 응답", "chat": "참가자 Agent 채팅", "admin": "총무 직접 입력", "seed": "시드"}


def stage_of(event: Event, now: datetime | None = None) -> str:
    now = now or clock.now()
    if now < event.attend_deadline:
        return "collecting"
    if now < event.payment_deadline:
        return "attend_closed"
    if now < event.event_at:
        return "payment_closed"
    return "event_done"


# ---------------------------------------------------------------- 기준표 저장(마감 시점)

def ensure_billing(db: Session, event: Event, now: datetime | None = None) -> bool:
    """납부 마감이 지났고 청구표가 없으면 팀별로 저장한다. 이미 있으면 그대로 둔다(수정 금지)."""
    now = now or clock.now()
    if now < event.payment_deadline:
        return False
    if db.scalar(select(func.count()).select_from(TeamBilling).where(TeamBilling.event_id == event.id)):
        return False
    responses.ensure_snapshot(db, event, now)
    deadline = event.payment_deadline
    for team in db.scalars(select(Team).where(Team.event_id == event.id).order_by(Team.code)):
        table = responses.fee_table(db, event, team.id, as_of=deadline)
        members = list(db.scalars(select(Participant).where(Participant.team_id == team.id).order_by(Participant.employee_no)))
        # 납부 마감 시점의 선택: 마감 기준 응답 + 그때까지 승인된 변경
        choice_at: dict[int, str | None] = {m.id: table[m.id].snapshot_choice for m in members}
        for c in db.scalars(
            select(ChangeLog).where(ChangeLog.participant_id.in_([m.id for m in members]), ChangeLog.after_deadline.is_(True), ChangeLog.status == "applied", ChangeLog.decided_at <= deadline).order_by(ChangeLog.id)
        ):
            choice_at[c.participant_id] = c.to_choice
        counts = {k: 0 for k in ("bus", "car", "absent", "undecided", "none")}
        lines = []
        for m in members:
            ch = choice_at[m.id]
            counts[ch or "none"] += 1
            lines.append({"participant_id": m.id, "name": m.name, "choice": ch, "fee": table[m.id].liability})
        paid = db.scalar(
            select(func.coalesce(func.sum(Allocation.amount), 0))
            .join(BankTransaction, BankTransaction.id == Allocation.transaction_id)
            .where(Allocation.team_id == team.id, Allocation.status != "cancelled", BankTransaction.txn_at <= deadline)
        )
        db.add(
            TeamBilling(
                event_id=event.id, team_id=team.id, billed=sum(l["fee"] or 0 for l in lines), counts=counts,
                unresolved=sum(1 for l in lines if l["fee"] is None), lines=lines, received_by_deadline=paid, captured_at=now,
            )
        )
    audit(db, event.id, "agent:settlement", "billing_captured", detail={"payment_deadline": clock.fmt(deadline)})
    db.flush()
    return True


def ensure_checkpoints(db: Session, event: Event, now: datetime | None = None) -> list[str]:
    """지난 마감의 기준표가 빠져 있으면 저장한다. 스케줄러·보고서 갱신·대시보드에서 부른다."""
    created = []
    if responses.ensure_snapshot(db, event, now):
        created.append("attend_snapshot")
    if ensure_billing(db, event, now):
        created.append("payment_billing")
    return created


# ---------------------------------------------------------------- 보고서 계산

def _people_and_team(db: Session, event: Event, team: Team, billing: TeamBilling | None) -> tuple[dict, list[dict]]:
    calc = compute_team(db, event, team)
    mgr = active_manager(db, team.id)
    mp = db.get(Participant, mgr.participant_id) if mgr else None
    billed_fee = {l["participant_id"]: l for l in (billing.lines if billing else [])}
    deadline = event.payment_deadline

    # 납부 마감 이후의 변동(당일 변동 포함)
    late_changes: dict[int, list[ChangeLog]] = {}
    for c in db.scalars(
        select(ChangeLog).where(ChangeLog.participant_id.in_([l["participant_id"] for l in calc["lines"]]), ChangeLog.after_deadline.is_(True)).order_by(ChangeLog.id)
    ):
        if c.status == "pending_approval" or (c.status == "applied" and (billing is None or (c.decided_at or c.requested_at) > deadline)):
            late_changes.setdefault(c.participant_id, []).append(c)

    people = []
    for l in calc["lines"]:
        pid = l["participant_id"]
        b = billed_fee.get(pid)
        base_fee = b["fee"] if b else None
        changes = late_changes.get(pid, [])
        pending = [c for c in changes if c.status == "pending_approval"]
        if b is not None and base_fee is None and l["fee"] is not None:
            base_fee = 0  # 청구 때 미응답·미정이라 청구액에 넣지 않았던 사람 → 최종 회비 전액이 차액
        diff = None if (l["fee"] is None or base_fee is None) else l["fee"] - base_fee
        kind = None
        if l["unresolved"]:
            kind = "unresolved"
        elif pending:
            kind = "pending"
        elif any(c.rule_missing and not c.admin_refund_decided for c in changes):
            kind = "rule_missing"
        elif diff and diff > 0:
            kind = "extra"
        elif diff and diff < 0:
            kind = "refund"
        elif billing is None and changes:
            kind = "changed"
        if kind is None:
            continue
        on_day = [c for c in changes if c.requested_at >= event.event_at]
        people.append(
            {
                "participant_id": pid, "name": l["name"], "team_id": team.id, "team_code": team.code, "team_name": team.name, "kind": kind,
                "snapshot_choice": l["snapshot_choice"], "billed_choice": b["choice"] if b else None, "choice": l["choice"],
                "billed_fee": base_fee, "final_fee": l["fee"], "amount": abs(diff) if diff else 0,
                "on_event_day": bool(on_day),
                "changes": [
                    {
                        "change_id": c.id, "from": CHOICE_LABELS[c.from_choice], "to": CHOICE_LABELS[c.to_choice], "status": c.status,
                        "requested_at": clock.fmt(c.requested_at), "source": SOURCE_LABEL.get(c.source, c.source), "rule": c.rule_text,
                        "refund_amount": c.refund_amount, "extra_charge": c.extra_charge, "note": c.note,
                        "on_event_day": c.requested_at >= event.event_at,
                    }
                    for c in changes
                ],
            }
        )

    proposed = sum(r["amount"] for r in calc["receipts"] if r["status"] == "proposed")
    paid_by_deadline = sum(r["amount"] for r in calc["receipts"] if r["status"] == "confirmed" and r["txn_at"] and r["txn_at"] <= clock.fmt(deadline))
    billed = billing.billed if billing else None
    balance = calc["balance"]
    if calc["blockers"]:
        status = "check"
    elif balance > 0:
        status = "extra"
    elif balance < 0:
        status = "refund"
    else:
        status = "done"
    row = {
        "team_id": team.id, "code": team.code, "name": team.name, "headcount": calc["headcount"],
        "manager": {"participant_id": mp.id, "name": mp.name, "slack_id": mp.slack_id} if mp else None,
        "counts": _counts(calc["lines"]),
        "billed": billed, "final_fee": calc["final_fee"], "received": calc["received"], "received_proposed": proposed,
        "paid_by_deadline": paid_by_deadline, "late_paid": calc["received"] - paid_by_deadline,
        "refunds_paid": calc["refunds_paid"],
        # 정산 잔액 = (청구액 − 확정 입금) + (최종 회비 − 청구액) + 지급 환불
        "payment_gap": None if billed is None else billed - calc["received"],
        "change_delta": None if billed is None else calc["final_fee"] - billed,
        "balance": balance, "status": status, "blockers": calc["blockers"],
        "people_counts": {k: sum(1 for p in people if p["kind"] == k) for k in ("extra", "refund", "pending", "unresolved", "rule_missing", "changed")},
    }
    return row, people


def _counts(lines: list[dict]) -> dict[str, int]:
    out = {k: 0 for k in ("bus", "car", "absent", "undecided", "none")}
    for l in lines:
        out[l["choice"] or "none"] += 1
    return out


def build(db: Session, event: Event, now: datetime | None = None) -> dict[str, Any]:
    """저장하지 않고 보고서 내용을 계산한다."""
    now = now or clock.now()
    billings = {b.team_id: b for b in db.scalars(select(TeamBilling).where(TeamBilling.event_id == event.id))}
    teams, people = [], []
    for team in db.scalars(select(Team).where(Team.event_id == event.id).order_by(Team.code)):
        row, ppl = _people_and_team(db, event, team, billings.get(team.id))
        teams.append(row)
        people.extend(ppl)
    counts = {k: sum(t["counts"][k] for t in teams) for k in ("bus", "car", "absent", "undecided", "none")}
    billed_total = sum(t["billed"] or 0 for t in teams) if billings else None
    summary = {
        "stage": stage_of(event, now), "stage_label": STAGES[stage_of(event, now)],
        "checkpoints": {
            "attend_snapshot_at": clock.fmt(event.snapshot_taken_at),
            "payment_billing_at": clock.fmt(min((b.captured_at for b in billings.values()), default=None)),
        },
        "headcount": sum(t["headcount"] for t in teams), "attending": counts["bus"] + counts["car"], "counts": counts,
        "teams": len(teams),
        "billed_total": billed_total,
        "final_total": sum(t["final_fee"] for t in teams),
        "received_total": sum(t["received"] for t in teams),
        "refunds_paid_total": sum(t["refunds_paid"] for t in teams),
        "extra_total": sum(t["balance"] for t in teams if t["status"] == "extra"),
        "refund_total": -sum(t["balance"] for t in teams if t["status"] == "refund"),
        "check_total": sum(abs(t["balance"]) for t in teams if t["status"] == "check"),
        "team_status": {k: sum(1 for t in teams if t["status"] == k) for k in ("extra", "refund", "check", "done")},
        "people_counts": {k: sum(1 for p in people if p["kind"] == k) for k in ("extra", "refund", "pending", "unresolved", "rule_missing", "changed")},
        "on_event_day_changes": sum(1 for p in people if p["on_event_day"]),
    }
    return {"summary": summary, "teams": teams, "people": people}


def _hash(content: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _diff(prev: SettlementReport | None, teams: list[dict], people: list[dict]) -> dict[str, Any]:
    if prev is None:
        return {"first": True, "teams": [], "people_added": [], "people_removed": []}
    old_t = {t["team_id"]: t for t in prev.teams}
    changed = []
    for t in teams:
        o = old_t.get(t["team_id"])
        if o is None or (o["balance"], o["status"], o["received"], o["final_fee"]) != (t["balance"], t["status"], t["received"], t["final_fee"]):
            changed.append({"team_id": t["team_id"], "name": t["name"], "before": {"balance": o["balance"], "status": o["status"]} if o else None, "after": {"balance": t["balance"], "status": t["status"]}})
    key = lambda p: (p["participant_id"], p["kind"], p["amount"])  # noqa: E731
    old_p = {key(p): p for p in prev.people}
    new_p = {key(p): p for p in people}
    return {
        "first": False,
        "teams": changed,
        "people_added": [{"name": p["name"], "team": p["team_name"], "kind": p["kind"], "amount": p["amount"]} for k, p in new_p.items() if k not in old_p][:50],
        "people_removed": [{"name": p["name"], "team": p["team_name"], "kind": p["kind"], "amount": p["amount"]} for k, p in old_p.items() if k not in new_p][:50],
    }


def latest(db: Session, event: Event) -> SettlementReport | None:
    return db.scalar(select(SettlementReport).where(SettlementReport.event_id == event.id).order_by(SettlementReport.version.desc()).limit(1))


def refresh(db: Session, event: Event, trigger: str, now: datetime | None = None) -> SettlementReport:
    """정산 Agent 의 보고서 갱신: 빠진 기준표 저장 → 다시 계산 → 이전 버전과 다르면 새 버전 저장 + 실행 기록."""
    now = now or clock.now()
    created = ensure_checkpoints(db, event, now)
    content = build(db, event, now)
    h = _hash({"teams": content["teams"], "people": content["people"], "stage": content["summary"]["stage"]})
    prev = latest(db, event)
    if prev is not None and prev.content_hash == h and not created:
        return prev
    rep = SettlementReport(
        event_id=event.id, version=(prev.version + 1) if prev else 1, trigger=trigger[:100], stage=content["summary"]["stage"],
        content_hash=h, summary=content["summary"], teams=content["teams"], people=content["people"],
        diff=_diff(prev, content["teams"], content["people"]), created_at=now,
    )
    db.add(rep)
    db.flush()
    s = content["summary"]
    text = (
        f"보고서 v{rep.version}: {s['stage_label']} · 추가 납부 {s['team_status']['extra']}팀 {s['extra_total']:,}원 · "
        f"환불 {s['team_status']['refund']}팀 {s['refund_total']:,}원 · 확인 필요 {s['team_status']['check']}팀"
    )
    db.add(
        AgentRun(
            event_id=event.id, agent="settlement", trigger=f"report:{trigger}"[:100],
            steps=[
                *({"tool": "save_checkpoint", "args": {"kind": c}, "result": "저장"} for c in created),
                {"tool": "build_report", "args": {}, "result": {"teams": len(content["teams"]), "people": len(content["people"])}},
                {"tool": "diff_previous", "args": {"previous": prev.version if prev else None}, "result": {"teams_changed": len(rep.diff.get("teams", []))}},
            ],
            rules=["정산 잔액 = (청구액 − 확정 입금) + (최종 회비 − 청구액) + 지급 환불", "금액이 같아도 근거 없이 팀을 확정하지 않음", "확인 과제가 남은 팀은 '확인 필요'"],
            result={"summary": text, "report_version": rep.version, "checkpoints": created},
            llm_mode="rule",
        )
    )
    return rep


def report_out(rep: SettlementReport) -> dict[str, Any]:
    return {
        "id": rep.id, "version": rep.version, "trigger": rep.trigger, "stage": rep.stage, "created_at": clock.fmt(rep.created_at),
        "summary": rep.summary, "teams": rep.teams, "people": rep.people, "diff": rep.diff,
    }


KIND_LABEL = {"extra": "추가 납부", "refund": "환불", "pending": "승인 대기", "unresolved": "응답 미확정", "rule_missing": "환불액 미정(총무 결정)", "changed": "변경 있음"}
STATUS_LABEL = {"extra": "추가 납부", "refund": "환불", "check": "확인 필요", "done": "정산 완료"}


def to_csv(rep: SettlementReport) -> str:
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"정산 보고서 v{rep.version}", rep.summary.get("stage_label", ""), clock.fmt(rep.created_at)])
    w.writerow([])
    w.writerow(["팀코드", "부서", "담당자", "인원", "청구액", "최종 회비", "확정 입금", "지급 환불", "정산 잔액", "상태", "확인할 것"])
    for t in rep.teams:
        w.writerow([t["code"], t["name"], (t["manager"] or {}).get("name", "미지정"), t["headcount"], t["billed"] if t["billed"] is not None else "", t["final_fee"], t["received"], t["refunds_paid"], t["balance"], STATUS_LABEL[t["status"]], " / ".join(t["blockers"])])
    w.writerow([])
    w.writerow(["부서", "이름", "구분", "청구 때 회비", "최종 회비", "차액", "변동 내역"])
    for p in rep.people:
        w.writerow([p["team_name"], p["name"], KIND_LABEL[p["kind"]], p["billed_fee"] if p["billed_fee"] is not None else "", p["final_fee"] if p["final_fee"] is not None else "미확정", p["amount"], " / ".join(f"{c['from']}→{c['to']}({c['source']}, {c['rule']})" for c in p["changes"])])
    return buf.getvalue()
