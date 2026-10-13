"""소통 Agent: 요청 해석 → 본인·팀 권한 확인 → 관련 기록 조회 → 부족한 정보 질문 → 확인 후 변경 기록.

대화 상태(ChatThread.state)
- idle                  : 대기
- awaiting_info         : 정보가 부족해 질문을 보낸 상태(pending 에 파악한 의도 저장)
- awaiting_confirmation : 갱신 도구 실행 직전 사용자 확인을 기다리는 상태(pending 에 도구·인자 저장)
상태는 DB 에 저장되므로 사용자가 나중에 돌아와도 이어서 답할 수 있다.
'네/아니요' 확인 판정과 갱신 실행은 LLM 이 아니라 코드가 한다.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..adapters.llm import NO, YES, AgentInput, LLMError, MockLLM, get_llm, validate_action
from ..models import AgentRun, ChatMessage, ChatThread, Event, Participant
from ..services.common import add_message, get_or_create_thread
from .tools import TOOLS, ToolContext, run_tool, tools_for

MAX_STEPS = 5
LABEL = {"bus": "참석+버스", "car": "참석+자차", "absent": "불참", "undecided": "미정", None: "미응답"}


def _won(v: int | None) -> str:
    return "미확정" if v is None else f"{v:,}원"


def _confirm_text(tool: str, dry: dict[str, Any]) -> str:
    if tool == "request_change":
        lines = [f"'{dry['from_label']}' → '{dry['to_label']}'(으)로 바꿀까요?"]
        if dry["after_deadline"] and dry.get("needs_admin_rule"):
            lines.append("- 환불액: 미정 — 이 시점에 맞는 환불 규칙이 없어 총무가 정합니다(0원으로 확정된 것이 아님)")
            lines.append("- 참석 마감 후 변경이라 총무 승인 후 반영됩니다.")
        elif dry["after_deadline"]:
            lines.append(f"- 회비 부담: {_won(dry['fee_before'])} → {_won(dry['fee_after'])}")
            if dry["refund_amount"]:
                lines.append(f"- 환불 예정: {_won(dry['refund_amount'])}")
            if dry["extra_charge"]:
                lines.append(f"- 추가 납부: {_won(dry['extra_charge'])}")
            lines.append(f"- 적용 규칙: {dry['rule_text']}")
            lines.append("- 참석 마감 후 변경이라 총무 승인 후 반영됩니다.")
        else:
            lines.append(f"- 회비: {_won(dry['fee_before'])} → {_won(dry['fee_after'])} (마감 전 변경)")
        lines.append("맞으면 '네', 아니면 '아니요'라고 답해 주세요.")
        return "\n".join(lines)
    if tool == "answer_task":
        return f"확인 요청 #{dry['task_id']}: {dry['question']}\n→ '{dry['option_label']}'(으)로 답할까요? ('네' / '아니요')"
    if tool == "request_correction":
        return f"다음 내용으로 총무에게 정정 요청을 보낼까요? ('네' / '아니요')\n“{dry['text']}”"
    return "진행할까요? ('네' / '아니요')"


def _done_text(tool: str, result: dict[str, Any]) -> str:
    if tool == "request_change":
        if result["status"] == "applied":
            return f"'{LABEL[result['to_choice']]}'(으)로 변경했어요. (변경 기록 #{result['change_id']})"
        return f"변경 요청을 접수했어요(#{result['change_id']}). 총무 승인 후 반영되고, 결과는 이 화면에서 볼 수 있어요."
    if tool == "answer_task":
        return f"답변을 기록했어요. 정산 Agent 재검토 결과: {result['review']['summary']}"
    if tool == "request_correction":
        return f"총무에게 정정 요청을 전달했어요(과제 #{result['task_id']})."
    return "처리했어요."


def is_yes(text: str) -> bool:
    return bool(YES.search(text)) and not NO.search(text) and len(text) <= 20


def is_no(text: str) -> bool:
    return bool(NO.search(text)) and len(text) <= 20


def _set_state(thread: ChatThread, state: str, pending: dict[str, Any] | None) -> None:
    thread.state, thread.pending, thread.updated_at = state, pending, clock.now()


def handle_message(db: Session, event: Event, actor: Participant, role: str, text: str) -> dict[str, Any]:
    text = (text or "").strip()[:1000]
    thread = get_or_create_thread(db, actor, role)
    user_msg = add_message(db, thread, "user", text)
    ctx = ToolContext(db=db, event=event, actor=actor, role=role, thread=thread, message_id=user_msg.id)
    steps: list[dict[str, Any]] = []  # 코드가 실행한 도구와 결과
    decisions: list[dict[str, Any]] = []  # 누가(LLM/규칙/코드) 어떤 행동을 골랐는지
    rules: list[str] = []
    notes: list[str] = []
    fallback: dict[str, str] = {}  # 실제 LLM 이 실패해 규칙 기반으로 대체된 경우 사유

    def llm_info() -> dict[str, Any]:
        configured = get_llm().mode
        used = sorted({d["by"].split(":")[0] for d in decisions})
        return {
            "configured": configured,
            "used": used,  # claude / gemini / mock / mock_fallback / code
            "model": next((d["by"].split(":", 1)[1] for d in decisions if d["by"].startswith(("gemini:", "claude:"))), ""),
            "fallback": fallback.get("reason", ""),
        }

    def finish(reply: str, outcome: str) -> dict[str, Any]:
        info = llm_info()
        msg = add_message(db, thread, "agent", reply, {"steps": steps, "state": thread.state, "rules": rules, "llm": info, "decisions": decisions})
        if thread.state == "awaiting_confirmation" and thread.pending:
            # '네'는 바로 이 확인 질문에만 묶인다. 사이에 다른 메시지(새 확인 요청 알림 등)가 끼면 다시 묻는다.
            thread.pending = {**thread.pending, "asked_message_id": msg.id}
        db.add(
            AgentRun(
                event_id=event.id,
                agent="communication",
                trigger=f"{role}_message",
                thread_id=thread.id,
                evidence_message_id=user_msg.id,
                steps=steps,
                rules=rules,
                result={"outcome": outcome, "reply_message_id": msg.id, "state": thread.state, "notes": notes, "decisions": decisions, "llm": info},
                llm_mode=("+".join(info["used"]) or info["configured"])[:20],
            )
        )
        return {"thread": serialize_thread(db, thread), "reply": serialize_message(msg)}

    llm = get_llm()

    # 1) 확인 대기 중이면 '네/아니요'는 코드가 판정해 실행·취소한다.
    if thread.state == "awaiting_confirmation" and thread.pending:
        tool, args = thread.pending["tool"], thread.pending["args"]
        decisions.append({"by": "code", "action": {"type": "confirmation_check", "tool": tool, "yes": is_yes(text), "no": is_no(text)}, "after_steps": 0})
        last_before = db.scalar(
            select(ChatMessage.id).where(ChatMessage.thread_id == thread.id, ChatMessage.id < user_msg.id).order_by(ChatMessage.id.desc()).limit(1)
        )
        if is_yes(text) and last_before != thread.pending.get("asked_message_id"):
            # 확인 질문 뒤에 새 알림·다른 답변이 끼어 있음 → 어떤 질문에 대한 '네'인지 모호하므로 실행하지 않고 다시 확인
            dry = run_tool(ctx, tool, args, dry=True)
            steps.append(dry)
            if not dry["ok"]:
                _set_state(thread, "idle", None)
                return finish(f"확인하던 요청을 더 진행할 수 없어요: {dry['error']['message']}", "reconfirm_failed")
            _set_state(thread, "awaiting_confirmation", {"tool": tool, "args": dry["args"], "preview": dry["result"]})
            return finish("확인 질문 뒤에 새 메시지가 와서, 어느 요청에 대한 '네'인지 다시 확인할게요.\n" + _confirm_text(tool, dry["result"]), "reconfirm")
        if is_yes(text):
            step = run_tool(ctx, tool, args)
            steps.append(step)
            _set_state(thread, "idle", None)
            if step["ok"]:
                if step["result"].get("rule"):
                    rules.append(step["result"]["rule"])
                return finish(_done_text(tool, step["result"]), "executed")
            return finish(f"처리하지 못했어요: {step['error']['message']}", "execute_failed")
        if is_no(text):
            _set_state(thread, "idle", None)
            return finish("알겠어요. 요청을 취소했고 아무것도 바꾸지 않았어요.", "cancelled")
        # 다른 말이면 확인 대기를 풀고 새 요청으로 해석한다.
        _set_state(thread, "idle", None)
        notes.append("확인 대기 중 다른 요청이 와서 이전 확인을 취소함")

    history = [
        {"sender": m.sender, "text": m.text}
        for m in db.scalars(select(ChatMessage).where(ChatMessage.thread_id == thread.id).order_by(ChatMessage.id.desc()).limit(7))
    ][::-1][:-1]

    # 2) LLM 이 다음 행동을 고르고, 코드가 검증·실행한다.
    for _ in range(MAX_STEPS):
        inp = AgentInput(
            role=role,
            message=text,
            state=thread.state,
            pending=thread.pending,
            steps=steps,
            tools=[t.contract() for t in tools_for(role)],
            history=history,
            actor_name=actor.name,
        )
        tool_names = {t["name"] for t in inp.tools}
        try:
            action = validate_action(llm.decide(inp), tool_names)
            by = f"{llm.mode}:{getattr(llm, 'last_model', '')}" if llm.mode in ("gemini", "claude") else ("mock_fallback" if fallback else "mock")
        except LLMError as e:
            # 실제 LLM 실패·형식 오류 → 이 턴의 나머지는 규칙 기반으로. 잘못된 출력은 실행하지 않는다.
            fallback["reason"] = str(e)[:200]
            notes.append(f"LLM 실패 → 규칙 기반 대체: {e}")
            llm = MockLLM()
            action = validate_action(llm.decide(inp), tool_names)
            by = "mock_fallback"
        decisions.append({"by": by, "action": action, "after_steps": len(steps)})  # 이 판단 전에 실행된 도구 수(순서 복원용)
        kind = action["type"]

        if kind == "tool":
            name, args = action.get("name"), action.get("args") or {}
            spec = TOOLS.get(name)
            if spec is not None and spec.mutating:
                dry = run_tool(ctx, name, args, dry=True)
                steps.append(dry)
                if not dry["ok"]:
                    continue  # 오류를 보고 LLM 이 답변을 고른다
                if dry["result"].get("rule_text"):
                    rules.append(dry["result"]["rule_text"])
                _set_state(thread, "awaiting_confirmation", {"tool": name, "args": dry["args"], "preview": dry["result"]})
                return finish(_confirm_text(name, dry["result"]), "awaiting_confirmation")
            steps.append(run_tool(ctx, name, args))
            continue
        if kind == "ask":
            pending = action.get("pending") if isinstance(action.get("pending"), dict) else {}
            _set_state(thread, "awaiting_info", {k: v for k, v in pending.items() if k in ("intent", "choice", "option")})
            return finish(str(action.get("text", "조금 더 알려 주세요."))[:1000], "awaiting_info")
        if kind == "reply":
            _set_state(thread, "idle", None)
            return finish(str(action.get("text", ""))[:2000] or "확인했어요.", "replied")
        break

    _set_state(thread, "idle", None)
    return finish("요청을 이해하지 못했어요. 총무에게 직접 문의해 주세요.", "gave_up")


def serialize_message(m: ChatMessage) -> dict[str, Any]:
    meta = m.meta or {}
    return {
        "id": m.id,
        "sender": m.sender,
        "text": m.text,
        "created_at": clock.fmt(m.created_at),
        "tools": [{"tool": s["tool"], "ok": s.get("ok"), "dry_run": s.get("dry_run", False)} for s in meta.get("steps", [])],
        "rules": meta.get("rules", []),
        "llm": meta.get("llm"),
    }


def serialize_thread(db: Session, thread: ChatThread) -> dict[str, Any]:
    msgs = db.scalars(select(ChatMessage).where(ChatMessage.thread_id == thread.id).order_by(ChatMessage.id))
    return {"id": thread.id, "role": thread.role, "state": thread.state, "pending": thread.pending, "messages": [serialize_message(m) for m in msgs]}


def thread_for(db: Session, actor: Participant, role: str) -> dict[str, Any]:
    return serialize_thread(db, get_or_create_thread(db, actor, role))
