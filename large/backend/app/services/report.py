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
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import clock
from ..models import CHOICE_LABELS, AgentRun, Allocation, BankTransaction, ChangeLog, DeadlineSnapshot, Event, EvidenceMessage, Participant, SettlementReport, Team, TeamBilling
from . import responses
from .common import active_manager, audit
from .settlement import compute_team

STAGES = {
    "collecting": "인원조사 중",
    "attend_closed": "참석 마감 → 회비 납부 중",
    "payment_closed": "납부 마감 → 행사 전",
    "event_done": "행사 종료 → 정산",
}
SOURCE_LABEL = {"web": "참가자 응답", "chat": "참가자 Agent 채팅", "admin": "총무 직접 입력", "seed": "시드", "slack": "슬랙 대화(정산 Agent 제안)", "admin_approved": "총무 승인"}


def on_event_day(event: Event, at: datetime) -> bool:
    """행사 당일(KST 날짜 기준) 이후의 일인지. 출발 전 아침 변동(08:35 버스 탑승 등)도 당일로 본다."""
    start = clock.kst(event.event_at).replace(hour=0, minute=0, second=0, microsecond=0)
    return at >= clock.to_utc_naive(start)


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

def _related_messages(msgs: list, team: Team, pid: int, name: str) -> list:
    """한 사람과 관련된 슬랙 메시지: 관련 ID·이름 언급·받는/보낸 사람 + 그 메시지 받은 사람의 1시간 안 답장(같은 부서)."""
    direct = [m for m in msgs if m.participant_id == pid or (m.team_id == team.id and (m.recipient == name or m.sender == name or f"{name}님" in m.text))]
    follow = [m for m in msgs if m.team_id == team.id and m not in direct and any(r.recipient and m.sender == r.recipient and timedelta(0) <= m.sent_at - r.sent_at <= timedelta(hours=1) for r in direct)]
    return sorted(direct + follow, key=lambda m: (m.sent_at, m.id))


def _basis(c: ChangeLog) -> tuple[str, str]:
    """변동 근거 구분: rule(환불 규칙·추가 전액 청구) | admin(총무 결정) | pending(규칙 없음 → 총무 판단 필요)"""
    if c.admin_refund_decided:
        return "admin", f"총무 결정: 환불 {c.refund_amount:,}원" + (f" ({c.note.split(' / ')[-1]})" if c.note else "")
    if c.rule_missing:
        return "pending", "환불 규칙 없음 → 총무 판단 필요"
    return "rule", c.rule_text


def _people_and_team(db: Session, event: Event, team: Team, billing: TeamBilling | None, msgs: list | None = None) -> tuple[dict, list[dict]]:
    calc = compute_team(db, event, team)
    mgr = active_manager(db, team.id)
    mp = db.get(Participant, mgr.participant_id) if mgr else None
    billed_fee = {l["participant_id"]: l for l in (billing.lines if billing else [])}
    deadline = event.payment_deadline
    msgs = msgs or []

    # 납부 마감 이후의 변동(당일 변동 포함) + 참석 마감~납부 마감 사이에 승인돼 청구에 이미 들어간 변동
    late_changes: dict[int, list[ChangeLog]] = {}
    included: dict[int, list[ChangeLog]] = {}
    for c in db.scalars(
        select(ChangeLog).where(ChangeLog.participant_id.in_([l["participant_id"] for l in calc["lines"]]), ChangeLog.after_deadline.is_(True)).order_by(ChangeLog.id)
    ):
        if c.status == "pending_approval" or (c.status == "applied" and (billing is None or (c.decided_at or c.requested_at) > deadline)):
            late_changes.setdefault(c.participant_id, []).append(c)
        elif c.status == "applied" and billing is not None:
            included.setdefault(c.participant_id, []).append(c)

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
        elif pid in included:
            kind, changes = "included", included[pid]
        if kind is None:
            continue
        on_day = [c for c in changes if on_event_day(event, c.requested_at)]
        rel = _related_messages(msgs, team, pid, l["name"])
        last = changes[-1] if changes else None
        basis_kind, basis_text = _basis(last) if last else ("none", "응답 없음 — 총무 확인 필요" if kind == "unresolved" else "")
        people.append(
            {
                "participant_id": pid, "name": l["name"], "team_id": team.id, "team_code": team.code, "team_name": team.name, "kind": kind,
                "manager_name": mp.name if mp else None,
                "snapshot_choice": l["snapshot_choice"], "billed_choice": b["choice"] if b else None, "choice": l["choice"],
                "billed_fee": base_fee, "final_fee": l["fee"], "amount": abs(diff) if diff else 0,
                "on_event_day": bool(on_day), "basis_kind": basis_kind, "basis": basis_text,
                "slack": ({"at": clock.fmt(rel[-1].sent_at), "sender": rel[-1].sender, "text": rel[-1].text, "id": rel[-1].id} if rel else None),
                "message_ids": [m.id for m in rel],
                "changes": [
                    {
                        "change_id": c.id, "from": CHOICE_LABELS[c.from_choice], "to": CHOICE_LABELS[c.to_choice], "status": c.status,
                        "requested_at": clock.fmt(c.requested_at), "decided_at": clock.fmt(c.decided_at), "source": SOURCE_LABEL.get(c.source, c.source), "rule": c.rule_text,
                        "basis_kind": _basis(c)[0], "basis": _basis(c)[1],
                        "refund_amount": c.refund_amount, "extra_charge": c.extra_charge, "note": c.note,
                        "on_event_day": on_event_day(event, c.requested_at),
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
    last_paid = max((r["txn_at"] for r in calc["receipts"] if r["status"] == "confirmed" and r["txn_at"]), default=None)
    for p in people:
        p["process"] = _process(p, status, last_paid, calc["refunds_paid"])
    row = {
        "team_id": team.id, "code": team.code, "name": team.name, "headcount": calc["headcount"],
        "manager": {"participant_id": mp.id, "name": mp.name, "slack_id": mp.slack_id} if mp else None,
        "counts": _counts(calc["lines"]),
        "billed": billed, "final_fee": calc["final_fee"], "received": calc["received"], "received_proposed": proposed,
        "paid_by_deadline": paid_by_deadline, "late_paid": calc["received"] - paid_by_deadline, "last_paid_at": last_paid,
        "refunds_paid": calc["refunds_paid"],
        # 정산 잔액 = (청구액 − 확정 입금) + (최종 회비 − 청구액) + 지급 환불
        "payment_gap": None if billed is None else billed - calc["received"],
        "change_delta": None if billed is None else calc["final_fee"] - billed,
        "balance": balance, "status": status, "blockers": calc["blockers"],
        "people_counts": {k: sum(1 for p in people if p["kind"] == k) for k in PEOPLE_KINDS},
    }
    return row, people


PEOPLE_KINDS = ("extra", "refund", "pending", "unresolved", "rule_missing", "changed", "included")


def _process(p: dict, team_status: str, last_paid: str | None, refunds_paid: int) -> str:
    """사람별 변동의 처리 상태(팀 단위 정산 기준)."""
    k = p["kind"]
    if k == "pending":
        return "승인 대기"
    if k == "unresolved":
        return "응답 미확정"
    if k == "rule_missing":
        return "총무 환불액 결정 필요"
    if k == "included":
        return "청구에 이미 포함"
    if team_status == "check":
        return "팀 확인 필요"
    day = f"({last_paid[5:10].replace('-', '/')})" if last_paid else ""
    if k == "extra":
        return {"done": f"팀 납부 완료{day}", "extra": "추가 납부 대기", "refund": "팀 환불액에서 차감"}.get(team_status, "")
    if k == "refund":
        return {"done": "환불 완료" if refunds_paid else "팀 정산 완료", "refund": "환불 대기", "extra": "팀 추가 납부에서 차감"}.get(team_status, "")
    return ""


def _counts(lines: list[dict]) -> dict[str, int]:
    out = {k: 0 for k in ("bus", "car", "absent", "undecided", "none")}
    for l in lines:
        out[l["choice"] or "none"] += 1
    return out


def build(db: Session, event: Event, now: datetime | None = None) -> dict[str, Any]:
    """저장하지 않고 보고서 내용을 계산한다."""
    now = now or clock.now()
    billings = {b.team_id: b for b in db.scalars(select(TeamBilling).where(TeamBilling.event_id == event.id))}
    msgs = list(db.scalars(select(EvidenceMessage).where(EvidenceMessage.event_id == event.id).order_by(EvidenceMessage.sent_at, EvidenceMessage.id)))
    teams, people = [], []
    for team in db.scalars(select(Team).where(Team.event_id == event.id).order_by(Team.code)):
        row, ppl = _people_and_team(db, event, team, billings.get(team.id), msgs)
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
        "people_counts": {k: sum(1 for p in people if p["kind"] == k) for k in PEOPLE_KINDS},
        "on_event_day_changes": sum(1 for p in people if p["on_event_day"]),
        "deadline_counts": _deadline_counts(db, event),
        "billed_final_delta": None if billed_total is None else sum(t["final_fee"] for t in teams) - billed_total,
    }
    return {"summary": summary, "teams": teams, "people": people}


def _deadline_counts(db: Session, event: Event) -> dict[str, int] | None:
    """참석 마감 기준 응답(DeadlineSnapshot) 인원. 마감 전이면 None."""
    if event.snapshot_taken_at is None:
        return None
    out = {k: 0 for k in ("bus", "car", "absent", "undecided", "none")}
    for ch in db.scalars(select(DeadlineSnapshot.choice).where(DeadlineSnapshot.event_id == event.id)):
        out[ch or "none"] += 1
    return out


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


KIND_LABEL = {"extra": "추가 납부", "refund": "환불", "pending": "승인 대기", "unresolved": "응답 미확정", "rule_missing": "환불액 미정(총무 결정)", "changed": "변경 있음", "included": "청구에 포함"}
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
    w.writerow(["부서", "담당자", "이름", "구분", "시점", "청구 때 회비", "최종 회비", "차액", "근거", "처리 상태", "슬랙(가장 최근)", "변동 내역"])
    for p in rep.people:
        sl = p.get("slack")
        w.writerow([
            p["team_name"], p.get("manager_name") or "", p["name"], KIND_LABEL[p["kind"]], "행사 당일" if p["on_event_day"] else "일반",
            p["billed_fee"] if p["billed_fee"] is not None else "", p["final_fee"] if p["final_fee"] is not None else "미확정",
            (-p["amount"] if p["kind"] == "refund" else p["amount"]), p.get("basis", ""), p.get("process", ""),
            f"{sl['sender']} {sl['at'][5:16].replace('T', ' ')}: {sl['text']}" if sl else "",
            " / ".join(f"{c['from']}→{c['to']}({c['source']}, {c['rule']})" for c in p["changes"]),
        ])
    return buf.getvalue()


def _signed_amount(p: dict) -> int | str:
    if p["kind"] == "included":
        return 0
    if p["kind"] in ("pending", "rule_missing", "unresolved") and not p["amount"]:
        return ""
    return -p["amount"] if p["kind"] == "refund" else p["amount"]


BASIS_KIND_LABEL = {"rule": "규칙", "admin": "총무 결정", "pending": "규칙 없음(총무 판단 전)", "none": ""}


def teams_csv(rep: SettlementReport) -> str:
    """부서별 정산표: 부서, 담당자, 청구, 변동 합계, 최종, 입금, 잔액, 상태, 근거 요약(변동 사람·사유)."""
    import csv
    import io

    by_team: dict[int, list[dict]] = {}
    for p in rep.people:
        if p["kind"] != "included":
            by_team.setdefault(p["team_id"], []).append(p)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["부서", "담당자", "청구", "변동 합계", "최종", "입금", "잔액", "상태", "근거 요약"])
    for t in rep.teams:
        reasons = []
        for p in by_team.get(t["team_id"], []):
            amt = _signed_amount(p)
            reasons.append(f"{p['name']} {KIND_LABEL[p['kind']]}" + (f" {amt:+,}원" if isinstance(amt, int) and amt else "") + (f"({p['basis']})" if p.get("basis") else ""))
        reasons += t["blockers"]
        delta = t["final_fee"] - t["billed"] if t["billed"] is not None else ""
        w.writerow([t["name"], (t["manager"] or {}).get("name", "미지정"), t["billed"] if t["billed"] is not None else "", delta, t["final_fee"], t["received"], t["balance"], STATUS_LABEL[t["status"]], " / ".join(reasons)])
    return buf.getvalue()


def people_csv(rep: SettlementReport) -> str:
    """사람별 변동표: 부서, 이름, 마감 기준, 현재, 금액, 근거(규칙/총무 결정/슬랙 한 줄), 처리 상태."""
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["부서", "이름", "마감 기준", "현재", "금액", "근거", "처리 상태"])
    for p in rep.people:
        sl = p.get("slack")
        label, basis = BASIS_KIND_LABEL.get(p.get("basis_kind") or "none", ""), p.get("basis", "")
        basis = basis if not label or basis.startswith(label) else f"{label}: {basis}" if basis else label
        if sl:
            basis += f" / 슬랙 {sl['sender']} {clock.kst(clock.parse_input(sl['at'])):%m/%d %H:%M}: {sl['text']}"
        w.writerow([p["team_name"], p["name"], CHOICE_LABELS[p["snapshot_choice"]], CHOICE_LABELS[p["choice"]], _signed_amount(p), basis, p.get("process", "")])
    return buf.getvalue()
