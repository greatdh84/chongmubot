"""정산 Agent: 불일치 발견 → 관련 기록 조회 → 확인 과제 생성 → 답변 재검토 → 정산안 갱신 또는 총무 이관.

판단 규칙(코드로 고정, 실행 기록에 그대로 남김)
R1 메모·입금자명에 팀코드/팀명이 정확히 한 팀 → 그 팀 입금으로 '제안'(총무 확정 필요)
R2 팀 표기가 없고 입금자명이 참가자 한 명과 일치
   - 그 사람이 팀 담당자이고 금액이 개인 회비와 다르면 → 팀 입금 제안
   - 금액이 본인 회비와 같으면 → 개인 직접 입금 제안(소속 팀 수납액에 포함)
   - 그 밖에는 → 담당자에게 확인 과제
R3 입금자명이 여러 참가자(동명이인)와 일치 → 후보 팀 담당자 모두에게 확인 과제
R4 아무 단서도 없음(입금자 불명) → 금액이 같은 미납 팀은 '참고 후보'로만 기록하고 그 팀 담당자에게 확인 과제.
   금액이 같다는 이유만으로 배정하지 않는다. 후보가 없거나 3팀 초과면 총무 이관.
R5 개인 입금과 팀 일괄 입금이 함께 있고 수납액이 회비를 넘으면 → 중복 납부 가능성 확인 과제
R6 마감 기준 응답이 미응답·미정인 팀원 → 담당자에게 알림 과제(미응답을 불참으로 처리하지 않음)
R7 입금자 불명·불일치 거래에 맞는 슬랙 입금 단서('ㅎㅂ 이름으로 보냈어요', 토스+가린 이름 = 보낸 사람)가 있으면
   그 부서를 '배정 후보'로 과제에 붙인다. 배정은 총무가 근거를 보고 지정해야 확정된다(accept_slack_suggestion).
답변 재검토: 후보 중 정확히 한 팀이 '네' → 그 팀 입금 제안 / 모두 '아니요' 또는 여러 팀 '네' → 총무 이관
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..models import AgentRun, Allocation, BankTransaction, ConfirmTask, Event, Participant, Team
from ..services import emails
from ..services.common import active_manager, add_message, create_task, get_or_create_thread, won
from ..services.payments import allocate, allocated_sum
from ..services.responses import fee_table
from ..services.settlement import compute_team

TEAM_CODE = re.compile(r"T\d{2}", re.I)
MAX_AMOUNT_CANDIDATES = 3
YES_NO = lambda yes, no: [{"key": "yes", "label": yes}, {"key": "no", "label": no}]  # noqa: E731


def _normalize_name(text: str) -> str:
    text = TEAM_CODE.sub("", text)
    return re.sub(r"[^가-힣A-Za-z]", "", text)


class Run:
    """한 번의 정산 Agent 실행에서 한 일(도구 호출·규칙·결과)을 모은다."""

    def __init__(self, db: Session, event: Event, trigger: str):
        self.db, self.event, self.trigger = db, event, trigger
        self.steps: list[dict[str, Any]] = []
        self.rules: set[str] = set()
        self.counts: dict[str, int] = {}

    def step(self, tool: str, args: dict[str, Any], result: Any, rule: str | None = None) -> None:
        self.steps.append({"tool": tool, "args": args, "result": result, "rule": rule})
        if rule:
            self.rules.add(rule)

    def count(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1

    def save(self, summary: str) -> dict[str, Any]:
        self.db.add(
            AgentRun(event_id=self.event.id, agent="settlement", trigger=self.trigger, steps=self.steps[:300], rules=sorted(self.rules), result={"summary": summary, "counts": self.counts}, llm_mode="rule")
        )
        return {"summary": summary, "counts": self.counts}


# ---------------------------------------------------------------- 도구(조회·질문·갱신)

def _teams(db: Session, event: Event) -> list[Team]:
    return list(db.scalars(select(Team).where(Team.event_id == event.id).order_by(Team.code)))


def find_teams_by_reference(teams: list[Team], txn: BankTransaction) -> list[Team]:
    text = f"{txn.memo} {txn.depositor}"
    codes = {c.upper() for c in TEAM_CODE.findall(text)}
    return [t for t in teams if t.code.upper() in codes or (t.name and t.name in text)]


def find_participants_by_name(db: Session, event: Event, depositor: str) -> list[Participant]:
    name = _normalize_name(depositor)
    if len(name) < 2:
        return []
    return list(db.scalars(select(Participant).where(Participant.event_id == event.id, Participant.name == name)))


def outstanding_including_proposed(db: Session, event: Event, team: Team) -> int:
    calc = compute_team(db, event, team)
    proposed = sum(r["amount"] for r in calc["receipts"] if r["status"] == "proposed")
    return calc["balance"] - proposed


def ask_manager(run: Run, *, team: Team, kind: str, dedupe_key: str, question: str, options: list[dict[str, str]], rule: str, evidence: dict[str, Any], transaction_id: int | None = None, participant_id: int | None = None, blocking: bool = True) -> ConfirmTask:
    db = run.db
    mgr = active_manager(db, team.id)
    assignee = "manager" if mgr else "admin"
    task, created = create_task(
        db,
        dedupe_key=dedupe_key,
        event_id=run.event.id,
        kind=kind,
        assignee=assignee,
        team_id=team.id,
        transaction_id=transaction_id,
        participant_id=participant_id,
        question=question if mgr else f"[담당자 미지정 → 총무 확인] {question}",
        options=options,
        rule=rule,
        evidence=evidence,
        created_by="settlement_agent",
        blocking=blocking,
    )
    if created:
        run.count(f"task:{kind}")
        run.step("create_confirm_task", {"team": team.code, "kind": kind, "assignee": assignee}, {"task_id": task.id}, rule)
        if mgr:
            thread = get_or_create_thread(db, db.get(Participant, mgr.participant_id), "manager")
            add_message(db, thread, "agent", f"[확인 요청 #{task.id}] {task.question}\n" + " / ".join(f"{o['label']}" for o in options), {"task_id": task.id})
            emails.enqueue_confirm_request(db, task)
    return task


def escalate(run: Run, *, kind: str, dedupe_key: str, question: str, rule: str, evidence: dict[str, Any], team_id: int | None = None, transaction_id: int | None = None) -> ConfirmTask:
    task, created = create_task(
        run.db,
        dedupe_key=dedupe_key,
        event_id=run.event.id,
        kind=kind,
        assignee="admin",
        status="escalated",
        team_id=team_id,
        transaction_id=transaction_id,
        question=question,
        options=[{"key": "done", "label": "총무가 처리함"}],
        rule=rule,
        evidence=evidence,
        created_by="settlement_agent",
    )
    if created:
        run.count("escalated")
        run.step("escalate_to_admin", {"kind": kind}, {"task_id": task.id}, rule)
    return task


def propose(run: Run, txn: BankTransaction, team: Team, amount: int, basis: str, confidence: str, rule: str, participant_id: int | None = None) -> Allocation:
    a = allocate(run.db, run.event, txn.id, team.id, amount, participant_id=participant_id, status="proposed", source="agent", basis=basis, confidence=confidence, actor="agent:settlement")
    run.count("proposed")
    run.step("propose_allocation", {"txn": txn.id, "team": team.code, "participant": participant_id, "amount": amount}, {"allocation_id": a.id}, rule)
    return a


# ---------------------------------------------------------------- 전체 점검

def _has_open_task_for_txn(db: Session, txn_id: int) -> bool:
    return db.scalar(select(ConfirmTask.id).where(ConfirmTask.transaction_id == txn_id, ConfirmTask.status.in_(("open", "answered", "escalated"))).limit(1)) is not None


def review_event(db: Session, event: Event, trigger: str = "manual") -> dict[str, Any]:
    run = Run(db, event, trigger)
    teams = _teams(db, event)
    fees_now = fee_table(db, event)

    # 입금 대조
    for txn in db.scalars(select(BankTransaction).where(BankTransaction.event_id == event.id).order_by(BankTransaction.txn_at, BankTransaction.id)):
        remaining = txn.amount - allocated_sum(db, txn.id)
        if remaining <= 0 or _has_open_task_for_txn(db, txn.id):
            continue
        evidence = {"transaction_id": txn.id, "depositor": txn.depositor, "amount": txn.amount, "memo": txn.memo, "txn_at": clock.fmt(txn.txn_at)}
        ref_teams = find_teams_by_reference(teams, txn)
        matches = find_participants_by_name(db, event, txn.depositor)
        run.step("lookup_transaction", {"txn": txn.id}, {"teams_in_memo": [t.code for t in ref_teams], "name_matches": [m.id for m in matches], "remaining": remaining})

        if len(ref_teams) == 1:
            team = ref_teams[0]
            member = next((m for m in matches if m.team_id == team.id), None)
            basis = f"메모/입금자에 팀 표기 '{team.code} {team.name}'" + (f", 입금자 {member.name}는 이 팀 소속" if member else ", 입금자명은 팀원과 불일치")
            propose(run, txn, team, remaining, basis, "high" if member else "medium", "R1 팀 표기 일치")
        elif len(ref_teams) > 1:
            escalate(run, kind="multiple_team_reference", dedupe_key=f"multi_ref:{txn.id}", question=f"입금 {txn.depositor} {won(txn.amount)}의 메모에 여러 팀이 적혀 있습니다({', '.join(t.code for t in ref_teams)}). 나눠 배정해 주세요.", rule="R1 여러 팀 표기 → 총무", evidence=evidence, transaction_id=txn.id)
        elif len(matches) == 1:
            p = matches[0]
            team = next(t for t in teams if t.id == p.team_id)
            mgr = active_manager(db, team.id)
            personal_fee = fees_now[p.id].liability
            if mgr and mgr.participant_id == p.id and remaining != personal_fee:
                propose(run, txn, team, remaining, f"입금자 {p.name} = {team.name} 담당자, 팀 표기 없음", "medium", "R2 담당자 명의 입금")
            elif personal_fee is not None and remaining == personal_fee:
                propose(run, txn, team, remaining, f"입금자 {p.name} = 참가자 본인, 금액 = 본인 회비 {won(personal_fee)}", "medium", "R2 개인 직접 입금", participant_id=p.id)
            else:
                ask_manager(
                    run, team=team, kind="unclear_deposit", dedupe_key=f"unclear:{txn.id}:{team.id}",
                    question=f"{p.name}님 이름으로 {won(txn.amount)} 입금({clock.kst(txn.txn_at):%m/%d})이 있습니다. {team.name} 회비로 반영할까요?",
                    options=YES_NO("네, 우리 팀 회비입니다", "아니요, 모르는 입금입니다"), rule="R2 이름 일치·금액 불명확 → 담당자 확인", evidence=evidence | {"participant_id": p.id}, transaction_id=txn.id,
                )
        elif len(matches) > 1:
            for p in matches:
                team = next(t for t in teams if t.id == p.team_id)
                ask_manager(
                    run, team=team, kind="ambiguous_depositor", dedupe_key=f"ambiguous:{txn.id}:{team.id}",
                    question=f"입금자 '{txn.depositor}' {won(txn.amount)}({clock.kst(txn.txn_at):%m/%d})은 동명이인이 있어 팀을 확정할 수 없습니다. {team.name} {p.name}님의 입금이 맞나요?",
                    options=YES_NO("네, 우리 팀원 입금입니다", "아니요"), rule="R3 동명이인 → 후보 팀 모두 확인", evidence=evidence | {"candidate_participant_ids": [m.id for m in matches]},
                    transaction_id=txn.id, participant_id=p.id,
                )
        else:
            candidates = [t for t in teams if outstanding_including_proposed(db, event, t) == remaining]
            run.step("find_amount_candidates", {"amount": remaining}, {"teams": [t.code for t in candidates], "note": "금액 일치는 참고용, 확정 근거 아님"}, "R4 입금자 불명")
            created = []
            if 1 <= len(candidates) <= MAX_AMOUNT_CANDIDATES:
                for team in candidates:
                    created.append(ask_manager(
                        run, team=team, kind="unknown_depositor", dedupe_key=f"unknown:{txn.id}:{team.id}",
                        question=f"입금자 '{txn.depositor}' {won(txn.amount)}({clock.kst(txn.txn_at):%m/%d}, 메모 '{txn.memo or '-'}')의 팀을 알 수 없습니다. 금액이 {team.name} 미납액과 같아 여쭤봅니다. 우리 팀이 보낸 입금인가요?",
                        options=YES_NO("네, 우리 팀이 보냈습니다", "아니요"), rule="R4 입금자 불명 → 금액 일치 후보 팀에 확인(금액만으로 확정 안 함)",
                        evidence=evidence | {"amount_candidates": [t.code for t in candidates]}, transaction_id=txn.id,
                    ))
            else:
                created.append(escalate(run, kind="unknown_depositor", dedupe_key=f"unknown_admin:{txn.id}", question=f"입금자 '{txn.depositor}' {won(txn.amount)}의 팀을 찾지 못했습니다(금액 일치 후보 {len(candidates)}팀). 총무 확인이 필요합니다.", rule="R4 후보 없음/과다 → 총무", evidence=evidence | {"amount_candidates": [t.code for t in candidates]}, transaction_id=txn.id))
            _attach(run, txn, created)

    # 중복 납부 가능성, 미확정 응답
    for team in teams:
        calc = compute_team(db, event, team)
        active = [r for r in calc["receipts"] if r["status"] in ("proposed", "confirmed")]
        total = sum(r["amount"] for r in active)
        has_team_level = any(r["participant_id"] is None for r in active)
        for r in active:
            if r["participant_id"] is None or not has_team_level or total - calc["final_fee"] < r["amount"]:
                continue
            p = db.get(Participant, r["participant_id"])
            ask_manager(
                run, team=team, kind="possible_duplicate", dedupe_key=f"dup:{r['allocation_id']}",
                question=f"{p.name}님 개인 입금 {won(r['amount'])}과 팀 일괄 입금이 함께 있어 팀 수납액이 회비보다 {won(total - calc['final_fee'])} 많습니다. 팀 일괄 입금에 {p.name}님 몫이 포함되어 있었나요?",
                options=YES_NO("네, 포함됨(중복 납부)", "아니요, 포함 안 됨"), rule="R5 개인+팀 입금 → 중복 가능성 확인",
                evidence={"allocation_id": r["allocation_id"], "team_received_incl_proposed": total, "final_fee": calc["final_fee"]}, participant_id=p.id,
            )
        if event.snapshot_taken_at is not None:
            unresolved = [l for l in calc["lines"] if l["unresolved"]]
            key = f"unresolved:{team.id}"
            existing = db.scalar(select(ConfirmTask).where(ConfirmTask.dedupe_key == key))
            if unresolved and existing is None:
                ask_manager(
                    run, team=team, kind="unresolved_response", dedupe_key=key,
                    question=f"참석 마감 기준으로 응답이 없거나 '미정'인 팀원이 {len(unresolved)}명 있습니다: {', '.join(l['name'] for l in unresolved)}. 본인 화면에서 응답하도록 안내해 주세요(자동으로 불참 처리하지 않습니다).",
                    options=[{"key": "yes", "label": "안내했습니다"}], rule="R6 미응답 자동 불참 금지", evidence={"participant_ids": [l["participant_id"] for l in unresolved]}, blocking=False,
                )
            elif not unresolved and existing is not None and existing.status != "resolved":
                existing.status, existing.resolution, existing.updated_at = "resolved", "모든 팀원 응답 확정", clock.now()
                run.count("auto_resolved")

    # 답변이 와 있는 과제 재검토
    for task in db.scalars(select(ConfirmTask).where(ConfirmTask.event_id == event.id, ConfirmTask.status == "answered")):
        _review(run, task)

    c = run.counts
    summary = f"배정 제안 {c.get('proposed', 0)}건, 확인 과제 {sum(v for k, v in c.items() if k.startswith('task:'))}건, 총무 이관 {c.get('escalated', 0)}건" + (f", 슬랙 근거 배정 후보 {c['slack_suggested']}건" if c.get("slack_suggested") else "")
    return run.save(summary)


# ---------------------------------------------------------------- R7 슬랙 입금 단서

def _attach(run: Run | None, txn: BankTransaction, tasks: list[ConfirmTask]) -> bool:
    """입금자 확인 과제에 슬랙 근거의 배정 후보를 붙인다(이미 붙어 있으면 그대로). 배정은 하지 않는다."""
    from ..services import slack

    db = run.db if run else None
    if db is None or not tasks:
        return False
    sug = slack.deposit_suggestion(db, run.event, txn.depositor)
    if sug is None:
        return False
    for t in tasks:
        if (t.evidence or {}).get("slack_suggestion", {}).get("message_id") == sug["message_id"]:
            continue
        t.evidence = {**(t.evidence or {}), "slack_suggestion": sug}
        t.question = t.question.split("\n[슬랙 근거]")[0] + f"\n[슬랙 근거] {sug['sender']} {clock.kst(clock.parse_input(sug['sent_at'])):%m/%d %H:%M} “{sug['text']}” → {sug['team']} 배정 후보(총무 지정 필요)"
        t.updated_at = clock.now()
    run.count("slack_suggested")
    run.step("match_slack_evidence", {"txn": txn.id, "depositor": txn.depositor}, {"team": sug["team"], "message_id": sug["message_id"], "why": sug["why"]}, "R7 슬랙 입금 단서 → 배정 후보(총무 지정)")
    return True


def attach_slack_suggestions(db: Session, event: Event) -> int:
    """슬랙 CSV 를 거래내역보다 늦게 넣었을 때: 이미 열린 입금자 확인 과제에 근거를 붙인다."""
    run = Run(db, event, "slack_attach")
    by_txn: dict[int, list[ConfirmTask]] = {}
    for t in db.scalars(select(ConfirmTask).where(ConfirmTask.event_id == event.id, ConfirmTask.kind.in_(("unknown_depositor", "deposit_unresolved")), ConfirmTask.status.in_(("open", "answered", "escalated")))):
        by_txn.setdefault(t.transaction_id, []).append(t)
    n = sum(_attach(run, db.get(BankTransaction, tid), ts) for tid, ts in by_txn.items() if tid)
    return n


def accept_slack_suggestion(db: Session, event: Event, task: ConfirmTask, admin_id: int) -> Allocation:
    """총무가 슬랙 근거를 보고 '이 부서 입금이 맞다'고 지정: 남은 금액을 확정 배정하고 그 거래의 과제를 모두 닫는다."""
    from ..errors import conflict

    sug = (task.evidence or {}).get("slack_suggestion")
    if task.transaction_id is None or not sug:
        raise conflict("NO_SUGGESTION", "슬랙 근거의 배정 후보가 없는 과제입니다")
    if task.status not in ("open", "answered", "escalated"):
        raise conflict("ALREADY_RESOLVED", "이미 처리된 과제입니다")
    txn = db.get(BankTransaction, task.transaction_id)
    remaining = txn.amount - allocated_sum(db, txn.id)
    if remaining <= 0:
        raise conflict("NOTHING_LEFT", "이미 모두 배정된 거래입니다")
    when = clock.kst(clock.parse_input(sug["sent_at"])).strftime("%m/%d %H:%M")
    a = allocate(db, event, txn.id, sug["team_id"], remaining, status="confirmed", source="admin", confidence="slack_evidence",
                 basis=f"슬랙 근거: {sug['sender']} {when} “{sug['text'][:60]}” ({sug['why']}) → 총무 지정", actor=f"admin:{admin_id}", admin_id=admin_id)
    for t in db.scalars(select(ConfirmTask).where(ConfirmTask.transaction_id == txn.id, ConfirmTask.status.in_(("open", "answered", "escalated")))):
        _resolve(t, f"총무가 슬랙 근거({sug['sender']} {when})로 {sug['team']} 입금 지정 → 확정 배정")
    return a


# ---------------------------------------------------------------- 답변 재검토

def review_task(db: Session, event: Event, task: ConfirmTask, trigger: str) -> dict[str, Any]:
    run = Run(db, event, trigger)
    _review(run, task)
    return run.save(_summary_of(run))


def _summary_of(run: Run) -> str:
    if run.counts.get("proposed"):
        return "입금 배정을 제안했어요. 총무가 확정하면 정산에 반영됩니다."
    if run.counts.get("escalated"):
        return "답변만으로 정할 수 없어 총무에게 넘겼어요."
    if run.counts.get("waiting"):
        return "다른 후보 팀의 답변을 기다리고 있어요."
    if run.counts.get("resolved"):
        return "확인 완료로 처리했어요."
    return "기록했어요."


def _resolve(task: ConfirmTask, text: str) -> None:
    task.status, task.resolution, task.updated_at = "resolved", text, clock.now()


def _review(run: Run, task: ConfirmTask) -> None:
    db, event = run.db, run.event
    if task.status != "answered":
        return
    rule_basis = f"담당자 답변(과제 #{task.id}, 메시지 #{task.answer_message_id or '-'})"
    if task.kind in ("unknown_depositor", "ambiguous_depositor"):
        siblings = list(db.scalars(select(ConfirmTask).where(ConfirmTask.transaction_id == task.transaction_id, ConfirmTask.kind == task.kind, ConfirmTask.status.in_(("open", "answered")))))
        if any(s.status == "open" for s in siblings):
            run.count("waiting")
            run.step("wait_for_other_candidates", {"txn": task.transaction_id}, {"open": [s.id for s in siblings if s.status == "open"]})
            return
        yes = [s for s in siblings if s.answer == "yes"]
        txn = db.get(BankTransaction, task.transaction_id)
        if len(yes) == 1:
            t = yes[0]
            remaining = txn.amount - allocated_sum(db, txn.id)
            if remaining > 0:
                propose(run, txn, db.get(Team, t.team_id), remaining, f"담당자 답변(과제 #{t.id}, 메시지 #{t.answer_message_id or '-'})으로 확인", "confirmed_by_manager", "답변 재검토: 한 팀만 '네'", participant_id=t.participant_id if task.kind == "ambiguous_depositor" else None)
            for s in siblings:
                _resolve(s, f"과제 #{t.id} 팀 입금으로 확인 → 배정 제안")
            run.count("resolved")
        else:
            reason = "후보 팀 모두 '아니요'" if not yes else f"여러 팀({len(yes)})이 '네'라고 답함"
            for s in siblings:
                _resolve(s, f"{reason} → 총무 이관")
            escalate(run, kind="deposit_unresolved", dedupe_key=f"deposit_unresolved:{txn.id}", question=f"입금 '{txn.depositor}' {won(txn.amount)}: {reason}. 총무가 직접 확인해 주세요.", rule="답변 재검토 → 총무 이관", evidence={"tasks": [s.id for s in siblings], "answers": {s.id: s.answer for s in siblings}}, transaction_id=txn.id)
    elif task.kind == "unclear_deposit":
        txn = db.get(BankTransaction, task.transaction_id)
        if task.answer == "yes":
            remaining = txn.amount - allocated_sum(db, txn.id)
            if remaining > 0:
                propose(run, txn, db.get(Team, task.team_id), remaining, f"{rule_basis}으로 팀 회비 확인", "confirmed_by_manager", "답변 재검토: 담당자 확인")
            _resolve(task, "담당자 확인 → 배정 제안")
            run.count("resolved")
        else:
            _resolve(task, "담당자가 모르는 입금이라고 답함 → 총무 이관")
            escalate(run, kind="deposit_unresolved", dedupe_key=f"deposit_unresolved:{txn.id}", question=f"입금 '{txn.depositor}' {won(txn.amount)}: 담당자가 모르는 입금이라고 답했습니다.", rule="답변 재검토 → 총무 이관", evidence={"task": task.id}, team_id=task.team_id, transaction_id=txn.id)
    elif task.kind == "possible_duplicate":
        if task.answer == "yes":
            _resolve(task, "중복 납부 확인 → 정산 잔액의 추가 환불로 처리(배정은 그대로 유지)")
            run.count("resolved")
        else:
            _resolve(task, "팀 입금에 포함 안 됐다는 답변 → 초과 수납 원인 불명, 총무 이관")
            escalate(run, kind="overpayment_unexplained", dedupe_key=f"overpay:{task.id}", question=f"{db.get(Team, task.team_id).name}: 수납액이 회비보다 많은데 담당자는 중복이 아니라고 답했습니다. 총무 확인이 필요합니다.", rule="답변 재검토 → 총무 이관", evidence={"task": task.id}, team_id=task.team_id)
    elif task.kind == "unresolved_response":
        run.count("noted")  # 안내 여부만 기록. 응답이 모두 확정되면 전체 점검에서 자동 해결
