"""슬랙 대화 → 근거 메시지(EvidenceMessage) → 정산 Agent 의 감지·판단·제안.

시연용 슬랙 대화 CSV(APP_MODE=demo 전용, 열: 일시, 보낸 사람, 받는 사람, 부서, 관련 ID, 내용)를 넣으면
1) 한 행이라도 틀리면(부서·ID 가 명단에 없음, 일시 형식 등) 줄 번호와 함께 전체 반려
2) 그 행사의 demo_slack 행을 지우고 다시 넣는다(멱등)
3) 정산 Agent 가 메시지를 읽는다(process_messages)
   - LLM_MODE=claude|gemini: LLM 이 JSON(사람, 변경 전→후, 시각, 근거 메시지 id, 확신도)으로 추출, 실패하면 규칙 기반으로 대체
   - LLM_MODE=mock: 같은 결과를 내는 규칙 추출
   추출 결과는 반드시 코드가 명단·현재 응답과 대조한 뒤에만 쓴다.
   - 참석 변경으로 보이는 메시지 → 이미 있는 변경 기록이면 근거로 잇고(linked), 없으면 변경 '제안'(승인 대기, source=slack)
   - 총무의 환불 결정 메시지 → 그 사람의 규칙 없는 변경에 '총무 결정' 근거(승인 화면에 환불액·사유 미리 채움)
   - 입금 단서('ㅎㅂ 이름으로 보냈어요', '토스로 보냈습니다') → 입금자 확인 과제에 배정 후보로 붙임(settlement_agent R7)
   자동 확정은 하지 않는다(HITL). 금액은 fees·settlement 코드가 계산한다.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from .. import clock
from ..adapters.llm import LLMError, complete_json, parse_choice
from ..config import settings
from ..errors import AppError, invalid
from ..models import CHOICE_LABELS, CHOICES, AgentRun, ChangeLog, ConfirmTask, EvidenceMessage, Event, Participant, Team
from . import responses
from .common import audit, won
from .roster import _decode

COLS = ("일시", "보낸 사람", "받는 사람", "부서", "내용")
BOT_NAMES = {"총무봇"}
ADMIN_NAMES = {"총무"}
KIND_LABEL = {
    "change": "참석 변경", "admin_refund": "총무 환불 결정", "deposit_hint": "입금 단서", "payment_promise": "납부 약속",
    "bot_dm": "총무봇 DM", "admin_note": "총무 메모", "note": "기타",
}
OUTCOME_LABEL = {
    "proposed": "변경 제안 생성(총무 승인 대기)", "linked": "이미 있는 변경 기록과 연결", "no_change": "현재 응답과 같아 변경 없음",
    "conflict": "다른 변경이 승인 대기 중이라 보류", "suggest_refund": "승인 화면에 총무 결정 환불액 제안", "before_deadline": "참석 마감 전 메시지(참가자 응답으로 처리)",
    "rejected": "검증 실패로 버림", "deposit_hint": "입금자 확인 과제의 근거로 대기", "recorded": "기록", "invalid": "검증 실패",
}


# ---------------------------------------------------------------- CSV

def _when(value: str) -> datetime:
    return clock.parse_input(value.replace(" ", "T") if "T" not in value else value)


def parse_csv(db: Session, event: Event, data: bytes) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(_decode(data)))
    header = [(h or "").strip() for h in (reader.fieldnames or [])]
    missing = [c for c in COLS if c not in header]
    if missing:
        raise invalid("CSV_COLUMNS", f"열이 없습니다: {', '.join(missing)}", [{"line": 1, "error": f"필요한 열: {', '.join(COLS)}, 관련 ID(선택)"}])
    teams = {t.name: t for t in db.scalars(select(Team).where(Team.event_id == event.id))}
    people = {p.employee_no: p for p in db.scalars(select(Participant).where(Participant.event_id == event.id))}
    rows, errors = [], []
    for line, raw in enumerate(reader, start=2):
        extra = raw.pop(None, None)  # 따옴표 없이 쉼표가 든 내용(예: 10,000원) → 넘친 칸을 내용 뒤에 다시 붙인다
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        if extra and header[-1] == "내용":
            row["내용"] = ",".join([row["내용"], *extra]).strip()
        if not any(row.values()):
            continue
        err = None
        team = teams.get(row["부서"]) if row["부서"] else None
        person = people.get(row.get("관련 ID", "")) if row.get("관련 ID") else None
        if row["부서"] and team is None:
            err = f"명단에 없는 부서: {row['부서']}"
        elif row.get("관련 ID") and person is None:
            err = f"명단에 없는 ID: {row['관련 ID']}"
        elif person is not None and team is not None and person.team_id != team.id:
            err = f"{row['관련 ID']} 은(는) {row['부서']} 소속이 아닙니다"
        elif not row["보낸 사람"] or not row["내용"]:
            err = "보낸 사람·내용은 비울 수 없습니다"
        at = None
        if err is None:
            try:
                at = _when(row["일시"])
            except ValueError:
                err = f"일시 형식 오류: '{row['일시']}' (예: 2026-10-09 09:40)"
        if err:
            errors.append({"line": line, "error": err})
            continue
        rows.append({"line": line, "sent_at": at, "sender": row["보낸 사람"][:50], "recipient": row["받는 사람"][:50], "team": team or (db.get(Team, person.team_id) if person else None), "participant": person, "text": row["내용"]})
    if errors:
        raise invalid("CSV_ROWS", f"슬랙 대화 {len(errors)}행에 오류가 있어 반영하지 않았습니다", errors[:50])
    if not rows:
        raise invalid("CSV_EMPTY", "슬랙 대화 CSV 에 메시지가 없습니다")
    return rows


def _sender_participant(db: Session, event: Event, name: str, team: Team | None) -> Participant | None:
    q = select(Participant).where(Participant.event_id == event.id, Participant.name == name)
    if team is not None:
        p = db.scalar(q.where(Participant.team_id == team.id))
        if p is not None:
            return p
    hits = list(db.scalars(q))
    return hits[0] if len(hits) == 1 else None


def import_csv(db: Session, event: Event, actor: str, data: bytes) -> dict[str, Any]:
    from . import report

    rows = parse_csv(db, event, data)
    clear(db, event)
    for r in rows:
        team = r["team"]
        sp = _sender_participant(db, event, r["sender"], team)
        db.add(
            EvidenceMessage(
                event_id=event.id, source="demo_slack", line=r["line"], sent_at=r["sent_at"], sender=r["sender"], recipient=r["recipient"],
                sender_participant_id=sp.id if sp else None, team_id=team.id if team else None,
                participant_id=r["participant"].id if r["participant"] else None, text=r["text"],
            )
        )
    db.flush()
    out = process_messages(db, event, trigger="slack_import")
    report.refresh(db, event, "slack_import")
    audit(db, event.id, actor, "demo_slack", detail={"messages": len(rows), **out["counts"]})
    return out


def clear(db: Session, event: Event) -> None:
    """그 행사의 demo_slack 메시지와, 그 메시지에서 나온 '아직 승인 안 된' 변경 제안을 지운다(승인된 변경은 남김)."""
    _drop_pending_proposals(db, event)
    db.execute(delete(EvidenceMessage).where(EvidenceMessage.event_id == event.id, EvidenceMessage.source == "demo_slack"))
    _drop_task_suggestions(db, event)
    db.flush()


def unlink_changes(db: Session, event: Event) -> None:
    """변경 기록을 지우기 전에(시연용 응답·변경 CSV 다시 넣기) 메시지 → 변경 연결을 끊는다."""
    db.execute(update(EvidenceMessage).where(EvidenceMessage.event_id == event.id).values(change_id=None))
    db.flush()


def _drop_pending_proposals(db: Session, event: Event) -> None:
    ids = list(db.scalars(select(ChangeLog.id).where(ChangeLog.event_id == event.id, ChangeLog.source == "slack", ChangeLog.status == "pending_approval")))
    unlink_changes(db, event)
    if ids:
        db.execute(delete(ConfirmTask).where(ConfirmTask.change_id.in_(ids)))
        db.execute(delete(ChangeLog).where(ChangeLog.id.in_(ids)))


def _drop_task_suggestions(db: Session, event: Event) -> None:
    for t in db.scalars(select(ConfirmTask).where(ConfirmTask.event_id == event.id, ConfirmTask.transaction_id.is_not(None))):
        if (t.evidence or {}).get("slack_suggestion"):
            ev = dict(t.evidence)
            ev.pop("slack_suggestion")
            t.evidence = ev
            t.question = t.question.split("\n[슬랙 근거]")[0]


# ---------------------------------------------------------------- 추출(규칙)

CONTRAST = re.compile(r"했었는데|했는데|었는데|였는데|지만|는데")
NOSHOW = re.compile(r"연락이?\s*안\s*(돼|되|됨)|안\s*나타|노쇼|안\s*왔")
ABSENT_EXTRA = re.compile(r"못\s*(온|올|와)|안\s*(온|올)")
HEDGE = re.compile(r"것 ?같|듯|아마")
REFUND_DECISION = re.compile(r"(\d[\d,]*)\s*원\s*(?:만\s*)?(?:환불|돌려)")
DEPOSIT_PAST = re.compile(r"보냈|입금했|이체했|송금했")
PROMISE = re.compile(r"보낼게|보내겠|입금할게|걷어서")
QUOTED = re.compile(r"[\'‘’\"“”]([^\'‘’\"“”]{1,20})[\'‘’\"“”]\s*(?:이름|명의)")
CHANNELS = ("토스", "카카오페이", "카카오뱅크")


def rule_extract(m: EvidenceMessage, event: Event) -> dict[str, Any]:
    text = m.text
    if m.sender in BOT_NAMES:
        return {"kind": "bot_dm", "confidence": "high", "reason": "총무봇이 보낸 DM"}
    if m.sender in ADMIN_NAMES:
        hit = REFUND_DECISION.search(text)
        if hit and m.participant_id and "환불" in text:
            return {"kind": "admin_refund", "amount": int(hit.group(1).replace(",", "")), "confidence": "high", "reason": f"총무가 '{hit.group(0)}' 이라고 답함"}
        return {"kind": "admin_note", "confidence": "high", "reason": "총무 메시지"}
    if m.participant_id:
        parts = CONTRAST.split(text)
        clause = parts[-1] if len(parts) > 1 else text
        choice, conf, why = parse_choice(clause), "high", "변경 표현"
        if choice is None and ABSENT_EXTRA.search(clause):
            choice, why = "absent", "'못 온다' 표현"
        if choice is None and NOSHOW.search(clause) and m.sent_at >= event.event_at:  # 출발 시각 이후 연락 두절만 노쇼 의심
            choice, conf, why = "absent", "low", "행사 당일 연락 두절 → 노쇼 의심"
        if choice == "undecided":
            choice = None
        if choice:
            if conf == "high" and HEDGE.search(text):
                conf = "medium"
            prev = parse_choice(parts[0]) if len(parts) > 1 else None
            return {"kind": "change", "to": choice, "from_said": prev, "confidence": conf, "reason": why + (" (앞 내용과 대조: '…는데' 뒤 문장 기준)" if len(parts) > 1 else "")}
    if DEPOSIT_PAST.search(text) and ("회비" in text or "원" in text):
        q = QUOTED.search(text)
        channel = next((c for c in CHANNELS if c in text), None)
        if q or channel:
            return {"kind": "deposit_hint", "depositor": q.group(1) if q else None, "channel": channel, "confidence": "high" if q else "medium",
                    "reason": f"입금자명 '{q.group(1)}' 언급" if q else f"'{channel}'(으)로 보냈다고 함"}
    if PROMISE.search(text):
        return {"kind": "payment_promise", "confidence": "high", "reason": "보내겠다는 약속"}
    return {"kind": "note", "confidence": "low", "reason": "변경·입금 단서 없음"}


# ---------------------------------------------------------------- 추출(LLM)

LLM_SYSTEM = """너는 회사 워크숍 총무를 돕는 '정산 Agent'다. 슬랙 대화 메시지를 읽고 메시지마다 하나씩 해석해 JSON 으로만 답한다.
kind:
- change: 참석 변경 요청·사실(누가 무엇에서 무엇으로). employee_no 는 people 의 사번, to 는 bus|car|absent 중 하나
- admin_refund: 보낸 사람이 '총무'이고 특정 사람의 환불액을 정한 말. amount 는 메시지에 적힌 원 단위 정수
- deposit_hint: 회비를 이미 보냈다는 말과 입금 단서. depositor(메시지에 적힌 입금자명) 또는 channel(토스 등)
- payment_promise: 앞으로 보내겠다는 말 / bot_dm: 총무봇이 보낸 안내 / admin_note: 총무의 다른 말 / note: 그 밖
규칙: 금액을 계산하지 않는다(메시지에 적힌 숫자만). 사람을 추측하지 않는다(related_id 가 있으면 그 사람). '불참이라고 했었는데 버스 탔어요'처럼
앞뒤가 바뀌면 마지막 사실(to=bus)을 쓴다. 행사 당일 '연락이 안 돼요'는 노쇼 의심 absent, confidence=low.
출력: {"items":[{"message_id":1,"kind":"change","employee_no":"E260022","from":"bus","to":"absent","at":"2026-10-09 09:40","amount":null,"depositor":null,"channel":null,"confidence":"high|medium|low","reason":"짧은 근거"}]}"""


def llm_extract(db: Session, event: Event, msgs: list[EvidenceMessage]) -> tuple[dict[int, dict[str, Any]], str]:
    pids = {m.participant_id for m in msgs if m.participant_id} | {m.sender_participant_id for m in msgs if m.sender_participant_id}
    people = []
    teams = {t.id: t.name for t in db.scalars(select(Team).where(Team.event_id == event.id))}
    cur = {}
    for p in db.scalars(select(Participant).where(Participant.id.in_(pids))):
        cur[p.employee_no] = p
        people.append({"employee_no": p.employee_no, "name": p.name, "team": teams[p.team_id], "current": responses.current_choice(db, p.id) or "none"})
    payload = {
        "event": {"attend_deadline": clock.fmt(event.attend_deadline), "payment_deadline": clock.fmt(event.payment_deadline), "event_at": clock.fmt(event.event_at)},
        "people": people,
        "messages": [{"message_id": m.id, "sent_at": clock.kst(m.sent_at).strftime("%Y-%m-%d %H:%M"), "sender": m.sender, "recipient": m.recipient,
                      "team": teams.get(m.team_id), "related_id": next((p["employee_no"] for p in people if cur[p["employee_no"]].id == m.participant_id), None), "text": m.text} for m in msgs],
    }
    out, model = complete_json(LLM_SYSTEM, payload)
    items = out.get("items") if isinstance(out, dict) else None
    if not isinstance(items, list):
        raise LLMError("items 목록이 없음")
    by_id: dict[int, dict[str, Any]] = {}
    for it in items:
        if not isinstance(it, dict) or not isinstance(it.get("message_id"), int):
            continue
        kind = it.get("kind")
        if kind not in KIND_LABEL:
            continue
        a: dict[str, Any] = {"kind": kind, "confidence": it.get("confidence") if it.get("confidence") in ("high", "medium", "low") else "medium", "reason": str(it.get("reason") or "")[:120]}
        if kind == "change":
            emp = it.get("employee_no")
            if emp not in cur or it.get("to") not in ("bus", "car", "absent"):
                a = {"kind": "note", "confidence": "low", "reason": f"LLM 추출 검증 실패(사번 {emp!s:.12}·to {it.get('to')!s:.8})", "llm_rejected": True}
            else:
                a |= {"to": it["to"], "from_said": it.get("from") if it.get("from") in CHOICES else None, "employee_no": emp, "participant_id": cur[emp].id}
        elif kind == "admin_refund":
            amt = it.get("amount")
            if not isinstance(amt, int) or amt < 0:
                a = {"kind": "admin_note", "confidence": "low", "reason": "LLM 환불액 검증 실패"}
            else:
                a["amount"] = amt
        elif kind == "deposit_hint":
            a |= {"depositor": (str(it["depositor"])[:30] if it.get("depositor") else None), "channel": (str(it["channel"])[:20] if it.get("channel") else None)}
        by_id[it["message_id"]] = a
    return by_id, model


# ---------------------------------------------------------------- 처리(검증 → 연결/제안)

def _kst(dt: datetime) -> str:
    return clock.kst(dt).strftime("%m/%d %H:%M")


def demo_messages(db: Session, event: Event) -> list[EvidenceMessage]:
    return list(db.scalars(select(EvidenceMessage).where(EvidenceMessage.event_id == event.id, EvidenceMessage.source == "demo_slack").order_by(EvidenceMessage.sent_at, EvidenceMessage.line)))


def _existing_change(db: Session, pid: int, to: str, at: datetime) -> ChangeLog | None:
    cands = list(db.scalars(select(ChangeLog).where(ChangeLog.participant_id == pid, ChangeLog.to_choice == to, ChangeLog.after_deadline.is_(True), ChangeLog.status.in_(("applied", "pending_approval")))))
    cands.sort(key=lambda c: abs((c.requested_at - at).total_seconds()))
    return cands[0] if cands and abs((cands[0].requested_at - at).total_seconds()) <= 2 * 86400 else None


def process_messages(db: Session, event: Event, trigger: str = "slack_import") -> dict[str, Any]:
    """메시지를 (다시) 읽어 해석·연결·제안. 여러 번 불러도 결과가 같다(이전 제안·연결을 지우고 다시 만든다)."""
    from ..agents.settlement_agent import attach_slack_suggestions

    _drop_pending_proposals(db, event)
    _drop_task_suggestions(db, event)
    msgs = demo_messages(db, event)
    counts: dict[str, int] = {"messages": len(msgs)}
    outcomes: dict[str, int] = {}
    steps: list[dict[str, Any]] = []
    if not msgs:
        return {"counts": counts, "outcomes": outcomes, "by": "rule", "fallback": ""}
    by, fallback, llm = "rule", "", {}
    if settings.llm_mode in ("claude", "gemini"):
        try:
            llm, model = llm_extract(db, event, msgs)
            by = f"{settings.llm_mode}:{model}"
        except (LLMError, AppError) as e:
            fallback = str(e)[:200]
            by = "mock_fallback"
    people = {p.id: p for p in db.scalars(select(Participant).where(Participant.event_id == event.id))}
    for m in msgs:
        a = llm.get(m.id) if llm else None
        if a is None:
            a = rule_extract(m, event)
            a["by"] = "rule" if by == "rule" or not llm else "rule(LLM 누락)"
        else:
            a["by"] = by
        if by == "mock_fallback":
            a["by"] = "mock_fallback"
        pid = a.pop("participant_id", None) or m.participant_id
        if m.participant_id and pid != m.participant_id:  # LLM 이 관련 ID 와 다른 사람을 고름 → 쓰지 않음
            a = {"kind": "note", "confidence": "low", "reason": "LLM 이 고른 사람이 관련 ID 와 달라 버림", "by": a["by"], "llm_rejected": True}
            pid = m.participant_id
        m.change_id = None
        kind = a["kind"]
        counts[kind] = counts.get(kind, 0) + 1
        if kind == "change":
            a |= _handle_change(db, event, m, people.get(pid), a)
        elif kind == "admin_refund":
            a |= _handle_admin_refund(db, event, m, people.get(pid), a)
        elif kind == "deposit_hint":
            sp = people.get(m.sender_participant_id)
            a |= {"outcome": "deposit_hint", "team_id": m.team_id or (sp.team_id if sp else None)}
        else:
            a["outcome"] = "recorded"
        if pid and not m.participant_id and kind in ("change", "admin_refund"):
            m.participant_id = pid
        a["kind_label"], a["outcome_label"] = KIND_LABEL[kind], OUTCOME_LABEL.get(a["outcome"], a["outcome"])
        m.analysis = a
        if kind == "change":
            outcomes[a["outcome"]] = outcomes.get(a["outcome"], 0) + 1
        steps.append({"tool": "read_message", "args": {"message_id": m.id, "line": m.line, "at": clock.fmt(m.sent_at)}, "result": {k: a.get(k) for k in ("kind", "to", "confidence", "outcome", "change_id", "by")}, "rule": a.get("reason")})
    db.flush()
    attach_slack_suggestions(db, event)
    summary = (
        f"슬랙 {len(msgs)}건 읽음 → 참석 변경 {counts.get('change', 0)}건(기존 기록 연결 {outcomes.get('linked', 0)} · 새 제안 {outcomes.get('proposed', 0)}"
        + "".join(f" · {OUTCOME_LABEL[k]} {v}" for k, v in outcomes.items() if k not in ("linked", "proposed")) + ")"
        f" · 총무 결정 {counts.get('admin_refund', 0)} · 입금 단서 {counts.get('deposit_hint', 0)} · 총무봇 DM {counts.get('bot_dm', 0)}"
    )
    db.add(AgentRun(
        event_id=event.id, agent="settlement", trigger=trigger, steps=steps,
        rules=["메시지 해석은 명단·현재 응답과 대조한 뒤에만 사용", "변경은 제안까지만(총무 승인 필요)", "금액은 환불 규칙 코드가 계산"],
        result={"summary": summary, "counts": counts, "outcomes": outcomes, "by": by, "fallback": fallback}, llm_mode=("rule" if by == "rule" else by.split(":")[0])[:20],
    ))
    return {"counts": counts, "outcomes": outcomes, "by": by, "fallback": fallback, "summary": summary}


def _handle_change(db: Session, event: Event, m: EvidenceMessage, p: Participant | None, a: dict[str, Any]) -> dict[str, Any]:
    if p is None:
        return {"outcome": "rejected", "check": "관련 ID 가 없어 누구의 변경인지 확정할 수 없음"}
    to = a["to"]
    current = responses.current_choice(db, p.id)
    out: dict[str, Any] = {"participant_name": p.name, "current": current, "current_label": CHOICE_LABELS[current]}
    if m.sent_at < event.attend_deadline:
        return out | {"outcome": "before_deadline"}
    existing = _existing_change(db, p.id, to, m.sent_at)
    if existing is not None:
        m.change_id = existing.id
        return out | {"outcome": "linked", "change_id": existing.id, "change_status": existing.status, "from": existing.from_choice,
                      "check": f"명단·현재 응답 대조: {p.name} {CHOICE_LABELS[existing.from_choice]}→{CHOICE_LABELS[to]} 변경 기록 #{existing.id} 와 일치"}
    if current == to:
        return out | {"outcome": "no_change", "check": f"현재 응답이 이미 {CHOICE_LABELS[to]}"}
    pending = db.scalar(select(ChangeLog).where(ChangeLog.participant_id == p.id, ChangeLog.status == "pending_approval"))
    if pending is not None:
        return out | {"outcome": "conflict", "check": f"다른 변경 #{pending.id}({CHOICE_LABELS[pending.to_choice]})이 승인 대기"}
    try:
        ch = responses.submit_change(
            db, event, p, to, source="slack", actor="agent:settlement", now=m.sent_at,
            note=f"슬랙 {m.sender} {_kst(m.sent_at)}: {m.text[:80]}",
            task_evidence={"slack_message_id": m.id, "slack": {"sender": m.sender, "sent_at": clock.fmt(m.sent_at), "text": m.text}, "confidence": a.get("confidence")},
        )
    except AppError as e:
        return out | {"outcome": "rejected", "check": e.message}
    if ch.status != "pending_approval":  # 마감 전 변경은 바로 반영되므로 여기 오지 않게 위에서 걸렀다
        return out | {"outcome": "rejected", "check": "마감 전 변경"}
    m.change_id = ch.id
    amt = "환불액 미정(규칙 없음 → 총무 판단)" if ch.rule_missing else f"환불 {won(ch.refund_amount)}" if ch.refund_amount else f"추가 {won(ch.extra_charge)}" if ch.extra_charge else "금액 변동 없음"
    return out | {"outcome": "proposed", "change_id": ch.id, "from": ch.from_choice, "amount_text": amt, "rule": ch.rule_text,
                  "check": f"명단·현재 응답 대조: {p.name} 현재 {CHOICE_LABELS[current]} → {CHOICE_LABELS[to]} 제안({amt})"}


def _handle_admin_refund(db: Session, event: Event, m: EvidenceMessage, p: Participant | None, a: dict[str, Any]) -> dict[str, Any]:
    if p is None:
        return {"outcome": "rejected", "check": "누구의 환불인지 관련 ID 가 없음"}
    c = db.scalar(select(ChangeLog).where(ChangeLog.participant_id == p.id, ChangeLog.after_deadline.is_(True), ChangeLog.status.in_(("pending_approval", "applied"))).order_by(ChangeLog.id.desc()))
    out: dict[str, Any] = {"participant_name": p.name}
    if c is None:
        return out | {"outcome": "rejected", "check": f"{p.name}님의 변경 기록이 없음"}
    m.change_id = c.id
    if c.status == "pending_approval":
        return out | {"outcome": "suggest_refund", "change_id": c.id, "suggested_refund": a["amount"], "check": f"승인 대기 변경 #{c.id}에 총무 결정 {won(a['amount'])} 제안"}
    return out | {"outcome": "linked", "change_id": c.id, "check": f"변경 #{c.id} 환불 {won(c.refund_amount)} — 총무 결정 근거"}


# ---------------------------------------------------------------- 입금 단서 → 입금자 확인 과제(정산 Agent R7)

def _mask_match(masked: str, name: str) -> bool:
    return len(masked) == len(name) and "*" in masked and all(a == "*" or a == b for a, b in zip(masked, name))


def deposit_suggestion(db: Session, event: Event, depositor: str) -> dict[str, Any] | None:
    """입금자 불명·불일치 거래에 맞는 슬랙 입금 단서를 찾는다. 금액이 아니라 '입금자명'·'채널+가린 이름'으로만 잇는다."""
    dep = re.sub(r"\s", "", depositor)
    for m in reversed(demo_messages(db, event)):
        a = m.analysis or {}
        if a.get("kind") != "deposit_hint" or not a.get("team_id"):
            continue
        why = None
        if a.get("depositor") and re.sub(r"\s", "", a["depositor"]) == dep:
            why = f"보낸 사람 {m.sender}이(가) 입금자명 '{a['depositor']}' 을(를) 직접 언급"
        elif a.get("channel") and dep.startswith(a["channel"]):
            masked = dep[len(a["channel"]):]
            if _mask_match(masked, m.sender):
                why = f"'{a['channel']}' 입금 + 가린 이름 '{masked}' = 보낸 사람 {m.sender}"
        if why:
            team = db.get(Team, a["team_id"])
            return {"message_id": m.id, "team_id": team.id, "team": team.name, "sender": m.sender, "sent_at": clock.fmt(m.sent_at), "text": m.text, "why": why, "by": a.get("by", "rule"), "confidence": a.get("confidence")}
    return None


def messages_by_change(db: Session, change_ids: list[int]) -> dict[int, list[EvidenceMessage]]:
    out: dict[int, list[EvidenceMessage]] = {}
    if change_ids:
        for m in db.scalars(select(EvidenceMessage).where(EvidenceMessage.change_id.in_(change_ids)).order_by(EvidenceMessage.sent_at)):
            out.setdefault(m.change_id, []).append(m)
    return out


def message_out(m: EvidenceMessage, names: dict[int, str] | None = None) -> dict[str, Any]:
    a = m.analysis or {}
    return {
        "id": m.id, "source": m.source, "sent_at": clock.fmt(m.sent_at), "sender": m.sender, "recipient": m.recipient, "team_id": m.team_id,
        "participant_id": m.participant_id, "participant_name": (names or {}).get(m.participant_id) if m.participant_id else None, "text": m.text,
        "analysis": {k: a.get(k) for k in ("kind", "kind_label", "to", "confidence", "by", "outcome", "outcome_label", "change_id", "check", "reason", "suggested_refund", "amount_text")} if a else None,
    }

