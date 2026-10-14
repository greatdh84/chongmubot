"""총무용 Agent: 총무가 묻는 말("경영지원팀의 추가 회비 납부자를 알려줘")을 정산 보고서 조회로 바꿔 답한다.

흐름: 질문 해석(LLM 또는 규칙) → 보고서 조회 도구 선택·실행(코드) → 결과로 답변
- 숫자는 모두 정산 보고서(services/report.py, 코드 계산)에서 온다. LLM 은 숫자를 만들지 않는다.
- 갱신 도구는 하나뿐: notify_managers(담당자에게 환불/추가 납부 안내 DM). 바로 실행하지 않고
  대상·금액을 먼저 보여 준 뒤 총무가 '네'라고 하면 코드가 발송 작업을 만든다(HITL).
- 매 질문마다 보고서를 먼저 갱신해(내용이 같으면 새 버전 없음) 최신 기록으로 답한다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..adapters.llm import NO, YES, AgentInput, LLMError, MockLLM, get_llm, validate_action
from ..errors import AppError, invalid, not_found
from ..models import AdminChat, AdminChatMessage, AgentRun, Event, Team
from ..services import emails, report as rpt
from . import admin_cards

MAX_STEPS = 5
KIND_LABEL = rpt.KIND_LABEL
STATUS_LABEL = rpt.STATUS_LABEL


@dataclass
class Ctx:
    db: Session
    event: Event
    rep: Any  # SettlementReport


@dataclass
class AdminTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    run: Callable[[Ctx, dict[str, Any]], dict[str, Any]]
    mutating: bool = False
    dry_run: Callable[[Ctx, dict[str, Any]], dict[str, Any]] | None = None

    def contract(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema, "mutating": self.mutating}


def _won(v: int | None) -> str:
    return "미확정" if v is None else f"{v:,}원"


def find_team(ctx: Ctx, query: str | None) -> dict | None:
    """'경영지원팀', '경영지원', 'T01' 모두 찾는다. 못 찾으면 NOT_FOUND."""
    if not query:
        return None
    q = re.sub(r"\s", "", str(query)).upper()
    teams = ctx.rep.teams
    for t in teams:
        if q == t["code"].upper() or q == t["name"].replace(" ", "").upper():
            return t
    hits = [t for t in teams if q.removesuffix("팀") and q.removesuffix("팀") in t["name"].replace(" ", "").upper()]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise invalid("AMBIGUOUS_TEAM", f"'{query}' 에 해당하는 부서가 여러 개입니다: {', '.join(t['name'] for t in hits)}")
    raise not_found(f"부서 '{query}'")


def _team_brief(t: dict) -> dict:
    return {
        "code": t["code"], "name": t["name"], "manager": (t["manager"] or {}).get("name"), "headcount": t["headcount"],
        "attending": t["counts"]["bus"] + t["counts"]["car"], "billed": t["billed"], "final_fee": t["final_fee"],
        "received": t["received"], "refunds_paid": t["refunds_paid"], "payment_gap": t["payment_gap"], "change_delta": t["change_delta"],
        "balance": t["balance"], "status": t["status"], "status_label": STATUS_LABEL[t["status"]], "blockers": t["blockers"],
    }


def _person_brief(p: dict) -> dict:
    return {
        "name": p["name"], "team": p["team_name"], "kind": p["kind"], "kind_label": KIND_LABEL[p["kind"]],
        "billed_fee": p["billed_fee"], "final_fee": p["final_fee"], "amount": p["amount"], "on_event_day": p["on_event_day"],
        "changes": [f"{c['from']}→{c['to']} ({c['source']}, {(c['requested_at'] or '')[5:16].replace('T', ' ')}{', 행사 당일' if c['on_event_day'] else ''}, {c['rule']}{', ' + c['note'] if c['note'] else ''})" for c in p["changes"]],
    }


# ---------------------------------------------------------------- 조회 도구

def get_report_summary(ctx: Ctx, args: dict) -> dict:
    s = ctx.rep.summary
    return {"report_version": ctx.rep.version, "stage": s["stage_label"], **{k: s.get(k) for k in ("headcount", "attending", "teams", "billed_total", "final_total", "received_total", "refunds_paid_total", "extra_total", "refund_total", "check_total", "team_status", "people_counts", "on_event_day_changes")}}


def get_department(ctx: Ctx, args: dict) -> dict:
    t = find_team(ctx, args.get("team"))
    if t is None:
        raise invalid("TEAM_REQUIRED", "어느 부서인지 알려 주세요")
    people = [_person_brief(p) for p in ctx.rep.people if p["team_id"] == t["team_id"]]
    return {"team": _team_brief(t), "people": people}


def list_people(ctx: Ctx, args: dict) -> dict:
    kind = args.get("kind") or "all"
    if kind not in (*KIND_LABEL, "all"):
        raise invalid("INVALID_KIND", f"kind 는 {', '.join(KIND_LABEL)} 또는 all")
    t = find_team(ctx, args.get("team"))
    rows = [p for p in ctx.rep.people if (kind == "all" or p["kind"] == kind) and (t is None or p["team_id"] == t["team_id"])]
    if args.get("on_event_day"):
        rows = [p for p in rows if p["on_event_day"]]
    return {"kind": kind, "kind_label": KIND_LABEL.get(kind, "전체"), "team": _team_brief(t) if t else None, "count": len(rows), "people": [_person_brief(p) for p in rows[:40]]}


def list_teams(ctx: Ctx, args: dict) -> dict:
    status = args.get("status") or "all"
    if status not in (*STATUS_LABEL, "all"):
        raise invalid("INVALID_STATUS", f"status 는 {', '.join(STATUS_LABEL)} 또는 all")
    rows = [t for t in ctx.rep.teams if status == "all" or t["status"] == status]
    return {"status": status, "status_label": STATUS_LABEL.get(status, "전체"), "count": len(rows), "total": sum(abs(t["balance"]) for t in rows), "teams": [_team_brief(t) for t in rows]}


def find_person(ctx: Ctx, args: dict) -> dict:
    name = str(args.get("name") or "").strip()
    if len(name) < 2:
        raise invalid("NAME_REQUIRED", "이름을 두 글자 이상 알려 주세요")
    from ..models import Participant
    from ..services.responses import fee_table

    out = []
    for p in ctx.db.scalars(select(Participant).where(Participant.event_id == ctx.event.id, Participant.name == name)):
        team = ctx.db.get(Team, p.team_id)
        st = fee_table(ctx.db, ctx.event, p.team_id)[p.id]
        hit = next((x for x in ctx.rep.people if x["participant_id"] == p.id), None)
        out.append({"participant_id": p.id, "name": p.name, "team": team.name, "choice": st.choice, "fee": st.liability, "in_report": _person_brief(hit) if hit else None})
    if not out:
        raise not_found(f"참가자 '{name}'")
    return {"count": len(out), "people": out}


def what_changed(ctx: Ctx, args: dict) -> dict:
    d = ctx.rep.diff or {}
    return {"report_version": ctx.rep.version, "trigger": ctx.rep.trigger, "created_at": clock.fmt(ctx.rep.created_at), **d}


def get_evidence(ctx: Ctx, args: dict) -> dict:
    """'왜?'의 근거: 청구(납부 마감 청구표) → 변동 줄(규칙·총무 결정, 요청·승인 일시, 슬랙 메시지) → 최종 회비, 입금 내역, 확인 필요 사유."""
    from ..models import Participant
    from ..services import evidence

    name = str(args.get("person") or "").strip()
    if name:
        hits = list(ctx.db.scalars(select(Participant).where(Participant.event_id == ctx.event.id, Participant.name == name)))
        if not hits:
            raise not_found(f"참가자 '{name}'")
        if len(hits) > 1:
            raise invalid("AMBIGUOUS_PERSON", f"'{name}' 동명이인이 있어요: " + ", ".join(ctx.db.get(Team, h.team_id).name for h in hits))
        e = evidence.person_evidence(ctx.db, ctx.event, hits[0])
    else:
        t = find_team(ctx, args.get("team"))
        if t is None:
            raise invalid("TEAM_REQUIRED", "어느 부서(또는 누구)의 근거인지 알려 주세요")
        e = evidence.team_evidence(ctx.db, ctx.event, ctx.db.get(Team, t["team_id"]))
        e["status"] = t["status"]
        e["status_label"] = STATUS_LABEL[t["status"]]
    bill = e.get("billing")
    return {
        "team": e["team"], "person": e.get("person"), "status_label": e.get("status_label"), "billing": {"formula": bill["formula"], "billed": bill["billed"], "unresolved_names": bill["unresolved_names"], "deadline": bill["deadline"]} if bill else None,
        "changes": [
            {"name": c["name"], "from": c["from"], "to": c["to"], "status": c["status"], "delta": c["delta"], "pending": c["pending_amount_text"], "basis": c["basis"], "basis_kind": c["basis_kind"],
             "requested_at": c["requested_at"], "decided_at": c["decided_at"], "source": c["source"], "on_event_day": c["on_event_day"],
             "messages": [{"sender": m["sender"], "sent_at": m["sent_at"], "text": m["text"]} for m in c["messages"]]}
            for c in e["changes"]
        ],
        "final_fee": e["final_fee"], "balance": e["balance"], "received": e.get("received"),
        "receipts": [{"txn_at": r["txn_at"], "depositor": r["depositor"], "amount": r["amount"], "status": r["status_label"], "late": r["late_label"]} for r in e.get("receipts", [])],
        "blockers": [b["text"] + (f" ({b['suggestion']})" if b.get("suggestion") else "") for b in e["blockers"]],
        "messages": [{"sender": m["sender"], "recipient": m["recipient"], "sent_at": m["sent_at"], "text": m["text"][:200], "source": m["source"]} for m in e["messages"][-6:]],
    }


# ---------------------------------------------------------------- 갱신 도구(확인 후 실행)

def _notify_targets(ctx: Ctx, args: dict) -> dict:
    kind = args.get("kind")
    if kind not in ("refund", "extra"):
        raise invalid("INVALID_KIND", "kind 는 refund(환불 안내) 또는 extra(추가 납부 안내)")
    t = find_team(ctx, args.get("team"))
    rows = [x for x in ctx.rep.teams if x["status"] == kind and (t is None or x["team_id"] == t["team_id"])]
    skipped = [x for x in ctx.rep.teams if (t is None or x["team_id"] == t["team_id"]) and x["status"] == "check" and ((kind == "refund" and x["balance"] < 0) or (kind == "extra" and x["balance"] > 0))]
    return {
        "kind": kind, "kind_label": "환불 안내" if kind == "refund" else "추가 납부 안내", "kind_obj": "환불 안내를" if kind == "refund" else "추가 납부 안내를",
        "targets": [{"team": x["name"], "manager": (x["manager"] or {}).get("name"), "amount": abs(x["balance"]), "team_id": x["team_id"]} for x in rows],
        "no_manager": [x["name"] for x in rows if not x["manager"]],
        "skipped_check": [x["name"] for x in skipped],
    }


def notify_dry(ctx: Ctx, args: dict) -> dict:
    out = _notify_targets(ctx, args)
    if not out["targets"]:
        raise AppError(409, "NO_TARGETS", f"{out['kind_obj']} 보낼 부서가 없습니다" + (f" (확인 필요로 보류: {', '.join(out['skipped_check'])})" if out["skipped_check"] else ""))
    return out


def notify_run(ctx: Ctx, args: dict) -> dict:
    out = notify_dry(ctx, args)
    kind = "refund_notice" if out["kind"] == "refund" else "extra_notice"
    from ..models import EmailJob

    team_ids = [x["team_id"] for x in out["targets"]]
    result = emails.enqueue_settlement_notices(ctx.db, ctx.event, kind, team_ids)
    sent = emails.dispatch_due(ctx.db, ctx.event, source="admin_agent")
    # 이번 안내 작업만 센다(같은 발송 실행에서 다른 대기 작업이 함께 나갈 수 있음)
    jobs = list(ctx.db.scalars(select(EmailJob).where(EmailJob.event_id == ctx.event.id, EmailJob.kind == kind, EmailJob.team_id.in_(team_ids))))
    status = {s: sum(1 for j in jobs if j.status == s) for s in ("sent", "queued", "retry_wait", "failed", "skipped")}
    return {**out, "created": result["created"], "dispatch": {"sent": status["sent"], "deferred": status["queued"] + status["retry_wait"], "failed": status["failed"], "skipped": status["skipped"], "notify_mode": sent.get("notify_mode")}}


TEAM_ARG = {"type": "string", "description": "부서 이름(예: 경영지원팀) 또는 팀코드(T01). 생략하면 전체"}
TOOLS: dict[str, AdminTool] = {
    t.name: t
    for t in [
        AdminTool("get_report_summary", "정산 보고서 전체 요약(단계, 인원, 청구·최종 회비, 입금, 추가 납부·환불 합계, 상태별 부서 수)", {"type": "object", "properties": {}}, get_report_summary),
        AdminTool("get_department", "한 부서의 정산 상태(청구액, 최종 회비, 확정 입금, 잔액, 확인할 것)와 변동이 있는 사람", {"type": "object", "properties": {"team": TEAM_ARG}, "required": ["team"]}, get_department),
        AdminTool(
            "list_people",
            "변동이 있는 사람 목록. kind: extra(추가 납부) refund(환불) pending(승인 대기) unresolved(응답 미확정) rule_missing(환불액 미정) changed(변경 있음) all",
            {"type": "object", "properties": {"kind": {"type": "string", "enum": [*KIND_LABEL, "all"]}, "team": TEAM_ARG, "on_event_day": {"type": "boolean", "description": "행사 당일 변동만"}}},
            list_people,
        ),
        AdminTool("list_teams", "상태별 부서 목록. status: extra(추가 납부) refund(환불) check(확인 필요) done(정산 완료) all", {"type": "object", "properties": {"status": {"type": "string", "enum": [*STATUS_LABEL, "all"]}}}, list_teams),
        AdminTool("find_person", "이름으로 참가자 한 명의 현재 응답·회비와 보고서 항목 찾기", {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}, find_person),
        AdminTool("what_changed", "지난 보고서 버전 이후 달라진 부서·사람", {"type": "object", "properties": {}}, what_changed),
        AdminTool(
            "get_evidence",
            "'왜?'의 근거: 부서(team) 또는 사람(person)의 청구→최종 계산(변동마다 규칙/총무 결정·요청/승인 일시·경로), 입금 내역(납부 마감 후 표시), 관련 슬랙 대화, 확인 필요 사유",
            {"type": "object", "properties": {"team": TEAM_ARG, "person": {"type": "string", "description": "사람 이름(부서 대신)"}}},
            get_evidence,
        ),
        AdminTool(
            "notify_managers",
            "환불(refund) 또는 추가 납부(extra) 대상 부서의 팀 담당자에게 Slack DM 안내. 개인에게는 보내지 않음. 실행 전 총무 확인 필요",
            {"type": "object", "properties": {"kind": {"type": "string", "enum": ["refund", "extra"]}, "team": TEAM_ARG}, "required": ["kind"]},
            notify_run, mutating=True, dry_run=notify_dry,
        ),
    ]
}


def run_tool(ctx: Ctx, name: str, args: dict, dry: bool = False) -> dict:
    spec = TOOLS[name]
    fn = spec.dry_run if dry and spec.dry_run else spec.run
    try:
        return {"tool": name, "args": args, "ok": True, "dry_run": dry, "result": fn(ctx, args)}
    except AppError as e:
        return {"tool": name, "args": args, "ok": False, "dry_run": dry, "error": {"code": e.code, "message": e.message}}


# ---------------------------------------------------------------- 대화

def _chat(db: Session, event: Event) -> AdminChat:
    c = db.scalar(select(AdminChat).where(AdminChat.event_id == event.id))
    if c is None:
        c = AdminChat(event_id=event.id)
        db.add(c)
        db.flush()
    return c


def _add(db: Session, chat: AdminChat, sender: str, text: str, meta: dict | None = None) -> AdminChatMessage:
    m = AdminChatMessage(chat_id=chat.id, sender=sender, text=text, meta=meta or {}, created_at=clock.now())
    db.add(m)
    db.flush()
    return m


def _confirm_text(dry: dict) -> str:
    lines = [f"다음 부서 담당자에게 {dry['kind_label']} Slack DM을 보낼까요?"]
    for x in dry["targets"]:
        lines.append(f"- {x['team']} · 담당자 {x['manager'] or '미지정(발송 안 됨)'} · {_won(x['amount'])}")
    if dry["skipped_check"]:
        lines.append(f"확인 필요로 보류(보내지 않음): {', '.join(dry['skipped_check'])}")
    lines.append("개인에게는 보내지 않고 담당자에게만 갑니다. 맞으면 '네', 아니면 '아니요'.")
    return "\n".join(lines)


def handle_message(db: Session, event: Event, text: str) -> dict:
    text = (text or "").strip()[:1000]
    if not text:
        raise invalid("EMPTY", "질문을 입력하세요")
    chat = _chat(db, event)
    user_msg = _add(db, chat, "user", text)
    rep = rpt.refresh(db, event, trigger="admin_agent")
    ctx = Ctx(db, event, rep)
    steps: list[dict] = []
    decisions: list[dict] = []
    notes: list[str] = []
    fallback: dict[str, str] = {}
    llm = get_llm()

    def finish(reply: str, outcome: str, card: dict | None = None, llm_card: dict | None = None) -> dict:
        used = sorted({d["by"].split(":")[0] for d in decisions})
        info = {"configured": get_llm().mode, "used": used, "model": next((d["by"].split(":", 1)[1] for d in decisions if ":" in d["by"]), ""), "fallback": fallback.get("reason", "")}
        if card is None and outcome == "replied":
            card = admin_cards_from_steps(ctx, steps)
            if card is not None and llm_card is not None:
                card, why = admin_cards.check_llm_card(llm_card, card, steps)
                if why:
                    notes.append(why)
                    card["llm_note"] = why
        if card is not None:
            card["tools"] = [s["tool"] for s in steps]
            reply = admin_cards.to_text(card)
        msg = _add(db, chat, "agent", reply, {"steps": steps, "llm": info, "decisions": decisions, "report_version": rep.version, "card": card})
        db.add(
            AgentRun(
                event_id=event.id, agent="admin", trigger="admin_message", steps=steps, rules=["숫자는 정산 보고서(코드 계산)에서만 인용"],
                result={"outcome": outcome, "summary": reply[:120], "decisions": decisions, "llm": info, "notes": notes, "report_version": rep.version},
                llm_mode=("+".join(used) or info["configured"])[:20],
            )
        )
        chat.updated_at = clock.now()
        return {"chat": serialize(db, chat), "reply": _msg_out(msg)}

    # 1) 확인 대기: '네/아니요'는 코드가 판정
    if chat.state == "awaiting_confirmation" and chat.pending:
        from .communication import is_no, is_yes

        tool, args = chat.pending["tool"], chat.pending["args"]
        decisions.append({"by": "code", "action": {"type": "confirmation_check", "tool": tool, "yes": is_yes(text), "no": is_no(text)}, "after_steps": 0})
        if is_yes(text):
            step = run_tool(ctx, tool, args)
            steps.append(step)
            chat.state, chat.pending = "idle", None
            if not step["ok"]:
                return finish(f"보내지 못했어요: {step['error']['message']}", "execute_failed")
            r = step["result"]
            return finish("", "executed", card=admin_cards.result_card(r))
        if is_no(text):
            chat.state, chat.pending = "idle", None
            return finish("알겠어요. 아무것도 보내지 않았어요.", "cancelled")
        chat.state, chat.pending = "idle", None
        notes.append("확인 대기 중 다른 질문이 와서 이전 확인을 취소함")

    history = [{"sender": m.sender, "text": m.text} for m in db.scalars(select(AdminChatMessage).where(AdminChatMessage.chat_id == chat.id).order_by(AdminChatMessage.id.desc()).limit(7))][::-1][:-1]
    context = {"teams": [{"code": t["code"], "name": t["name"], "manager": (t["manager"] or {}).get("name")} for t in rep.teams], "stage": rep.summary["stage_label"], "report_version": rep.version}
    for _ in range(MAX_STEPS):
        inp = AgentInput(role="admin", message=text, state=chat.state, pending=chat.pending, steps=steps, tools=[t.contract() for t in TOOLS.values()], history=history, actor_name="총무", context=context)
        names = set(TOOLS)
        try:
            action = validate_action(llm.decide(inp), names)
            by = f"{llm.mode}:{getattr(llm, 'last_model', '')}" if llm.mode in ("gemini", "claude") else ("mock_fallback" if fallback else "mock")
        except LLMError as e:
            fallback["reason"] = str(e)[:200]
            notes.append(f"LLM 실패 → 규칙 기반 대체: {e}")
            llm = MockLLM()
            action = validate_action(llm.decide(inp), names)
            by = "mock_fallback"
        decisions.append({"by": by, "action": action, "after_steps": len(steps)})
        if action["type"] == "tool":
            spec = TOOLS[action["name"]]
            if spec.mutating:
                dry = run_tool(ctx, spec.name, action["args"], dry=True)
                steps.append(dry)
                if not dry["ok"]:
                    continue
                chat.state, chat.pending = "awaiting_confirmation", {"tool": spec.name, "args": action["args"]}
                card = admin_cards.confirm_card(dry["result"])
                card["tools"] = [s["tool"] for s in steps]
                msg = finish(_confirm_text(dry["result"]), "awaiting_confirmation", card=card)
                return msg
            steps.append(run_tool(ctx, spec.name, action["args"]))
            continue
        chat.state, chat.pending = "idle", None
        return finish(action["text"], "replied" if action["type"] == "reply" else "asked", llm_card=action.get("card"))
    return finish("질문을 처리하지 못했어요. 부서 이름과 알고 싶은 것(추가 납부, 환불, 확인 필요 등)을 함께 적어 주세요.", "gave_up")


def admin_cards_from_steps(ctx: Ctx, steps: list[dict]) -> dict | None:
    """마지막으로 성공한 조회 도구 결과로 보고 카드(템플릿)를 만든다. 숫자는 코드 계산."""
    ok = [s for s in steps if s.get("ok") and not s.get("dry_run")]
    if not ok:
        return None
    last = ok[-1]
    r, tool = last["result"], last["tool"]
    db, ev, rep = ctx.db, ctx.event, ctx.rep
    if tool == "get_evidence":
        if r.get("person"):
            return admin_cards.person_card(db, ev, rep, r["person"]["participant_id"])
        return admin_cards.team_card(db, ev, rep, r["team"]["team_id"])
    if tool == "get_department":
        t = next(x for x in rep.teams if x["code"] == r["team"]["code"])
        return admin_cards.team_card(db, ev, rep, t["team_id"])
    if tool == "list_teams":
        return admin_cards.teams_card(db, ev, rep, r["status"])
    if tool == "list_people":
        return admin_cards.people_card(rep, r)
    if tool == "find_person" and r["count"] == 1:
        return admin_cards.person_card(db, ev, rep, r["people"][0]["participant_id"])
    if tool == "get_report_summary":
        return admin_cards.summary_card(db, ev, rep)
    if tool == "what_changed":
        return admin_cards.changed_card(r)
    return None


def report_action(db: Session, event: Event, *, kind: str, team_id: int | None, what: str) -> dict:
    """총무가 화면에서 승인·지정·확정한 뒤 Agent 가 채팅에 남기는 짧은 결과 보고(보고서는 호출 전에 갱신돼 있어야 함)."""
    rep = rpt.latest(db, event)
    card = admin_cards.after_action(db, event, rep, kind=kind, team_id=team_id, what=what)
    chat = _chat(db, event)
    msg = _add(db, chat, "agent", admin_cards.to_text(card), {"card": card, "report_version": rep.version, "action_report": kind})
    chat.updated_at = clock.now()
    return _msg_out(msg)


def _msg_out(m: AdminChatMessage) -> dict:
    meta = m.meta or {}
    return {
        "id": m.id, "sender": m.sender, "text": m.text, "created_at": clock.fmt(m.created_at), "card": meta.get("card"),
        "tools": [{"tool": s["tool"], "ok": s.get("ok"), "dry_run": s.get("dry_run", False)} for s in meta.get("steps", [])],
        "rules": [], "llm": meta.get("llm"), "report_version": meta.get("report_version"),
    }


def serialize(db: Session, chat: AdminChat) -> dict:
    msgs = db.scalars(select(AdminChatMessage).where(AdminChatMessage.chat_id == chat.id).order_by(AdminChatMessage.id))
    return {"id": chat.id, "role": "admin", "state": chat.state, "pending": chat.pending, "messages": [_msg_out(m) for m in msgs]}


def thread_for(db: Session, event: Event) -> dict:
    return serialize(db, _chat(db, event))
