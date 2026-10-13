"""LLM 어댑터. LLM 은 '다음 행동'만 고르고(도구 호출 / 추가 질문 / 답변), 금액 계산·권한·기록은 코드가 한다.

행동(Action) 계약 — LLM 출력은 반드시 아래 JSON 중 하나:
  {"type": "tool",  "name": "<도구 이름>", "args": {...}}
  {"type": "ask",   "text": "<사용자에게 물을 말>", "pending": {"intent": "...", ...}}
  {"type": "reply", "text": "<최종 답변>"}

LLM_MODE=mock  : 규칙 기반 MockLLM (외부 키 불필요, 결정적)
LLM_MODE=gemini: Gemini REST 호출. 실패하면 MockLLM 으로 대체하고 그 사실을 실행 기록에 남긴다.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from ..config import settings

LABEL = {"bus": "참석+버스", "car": "참석+자차", "absent": "불참", "undecided": "미정", None: "미응답"}


@dataclass
class AgentInput:
    role: str  # participant | manager
    message: str
    state: str
    pending: dict[str, Any] | None
    steps: list[dict[str, Any]]  # 이번 턴에 실행한 도구 [{tool, args, ok, result|error}]
    tools: list[dict[str, Any]]  # 사용할 수 있는 도구 계약
    history: list[dict[str, str]] = field(default_factory=list)
    actor_name: str = ""


class LLMError(Exception):
    pass


class LLM(Protocol):
    mode: str

    def decide(self, inp: AgentInput) -> dict[str, Any]: ...


# ------------------------------------------------------------------ 규칙 기반 모의 LLM

CHANGE_WORDS = re.compile(r"변경|바꿔|바꾸|바뀌|수정|취소|참석할|갈게|갈께|갑니다|못 ?가|안 ?가|못 ?타|못 ?탈|안 ?타|안 ?탈")
STATUS_WORDS = re.compile(r"현재|내 ?응답|상태|조회|확인|뭐로|어떻게 ?되")
FEE_WORDS = re.compile(r"회비|얼마|금액|환불|돈|납부")
YES = re.compile(r"^\s*(네|예|응|넵|넹|맞아|맞습니다|맞아요|좋아|좋아요|그래|그렇게 해|진행해|ㅇㅇ|yes|ok)", re.I)
NO = re.compile(r"^\s*(아니|아뇨|아닙|취소|그만|안 ?할|no)", re.I)
POSITIVE = re.compile(r"네|예|맞|포함|우리 ?팀|저희 ?팀|입금했|보냈")
NEGATIVE = re.compile(r"아니|아뇨|아닙|없|모르|안 ?했|미포함|포함 ?안|다른 ?팀")
CORRECTION_WORDS = re.compile(r"정정|잘못|틀렸|틀려|오류|이상해|안 ?맞")
TASK_WORDS = re.compile(r"확인 ?요청|과제|질문|답변|물어")
SUMMARY_WORDS = re.compile(r"현황|명세|청구|잔액|정산|얼마|응답|미납|입금")
NAME_TARGET = re.compile(r"([가-힣]{2,4})\s*(?:님|씨)(?:의|도|이|가|은|는|을|를)?\s")


def parse_choice(text: str) -> str | None:
    t = text.replace(" ", "")
    if re.search(r"자차|자가용|차가지고|차로|운전|차타고|제차", t):
        return "car"
    if re.search(r"불참|못가|안가|못갈|안갈|빠질|빠져|참석못|참석안|못와|불가", t):
        return "absent"
    if re.search(r"미정|모르겠|아직|고민", t):
        return "undecided"
    if "버스" in t and not re.search(r"버스(를|는|도|가)?(취소|안타|안탈|못타|못탈|말고|안|못)", t):
        return "bus"
    return None


def parse_target(text: str, actor_name: str) -> str | None:
    if "대신" not in text and "팀원" not in text and not NAME_TARGET.search(text + " "):
        return None
    m = NAME_TARGET.search(text + " ")
    if m and m.group(1) != actor_name:
        return m.group(1)
    if "대신" in text or "팀원" in text:
        return "다른 사람"
    return None


def _won(v: Any) -> str:
    return "미확정" if v is None else f"{int(v):,}원"


def _error_reply(err: dict[str, Any]) -> str:
    code = err.get("code")
    if code == "FORBIDDEN":
        return "본인의 응답만 확인·변경할 수 있어요. 다른 분의 변경은 그분이 직접 요청하거나 총무에게 문의해 주세요."
    return err.get("message", "요청을 처리하지 못했어요. 총무에게 문의해 주세요.")


class MockLLM:
    mode = "mock"

    def decide(self, inp: AgentInput) -> dict[str, Any]:
        return self._participant(inp) if inp.role == "participant" else self._manager(inp)

    # 참가자: 상태 조회 / 회비 문의 / 변경 요청
    def _participant(self, inp: AgentInput) -> dict[str, Any]:
        msg = inp.message
        steps = inp.steps
        if steps:
            last = steps[-1]
            if not last["ok"]:
                return {"type": "reply", "text": _error_reply(last["error"])}
            done = {s["tool"] for s in steps}
            if FEE_WORDS.search(msg) and "get_fee_rules" not in done:
                return {"type": "tool", "name": "get_fee_rules", "args": {}}
            status = next((s["result"] for s in steps if s["tool"] == "get_my_status"), None)
            rules = next((s["result"] for s in steps if s["tool"] == "get_fee_rules"), None)
            lines = []
            if status:
                lines.append(f"현재 응답은 '{status['choice_label']}'이고, 회비는 {_won(status['fee'])}입니다.")
                if status.get("pending_change"):
                    pc = status["pending_change"]
                    lines.append(f"총무 승인을 기다리는 변경 요청이 있어요: {LABEL[pc['from']]} → {LABEL[pc['to']]}.")
                lines.append(f"참석 응답 마감: {status['attend_deadline']}")
            if rules:
                f = rules["fees"]
                lines.append(f"회비는 버스 {f['bus']:,}원, 자차 {f['car']:,}원, 불참 {f['absent']:,}원입니다.")
                if rules["refund_rules"]:
                    lines.append("마감 후 변경 환불 규칙: " + " / ".join(r["description"] for r in rules["refund_rules"]))
            return {"type": "reply", "text": "\n".join(lines) or "확인했어요."}

        choice = parse_choice(msg)
        target = parse_target(msg, inp.actor_name)
        if inp.state == "awaiting_info" and (inp.pending or {}).get("intent") == "change":
            if choice:
                return {"type": "tool", "name": "request_change", "args": {"choice": choice}}
            return {"type": "ask", "text": "버스, 자차, 불참, 미정 중 어떤 것으로 바꿀까요?", "pending": {"intent": "change"}}
        if choice or CHANGE_WORDS.search(msg):
            if target:
                return {"type": "tool", "name": "preview_change", "args": {"choice": choice or "absent", "target": target}}
            if choice:
                return {"type": "tool", "name": "request_change", "args": {"choice": choice}}
            if "버스" in msg:
                return {"type": "ask", "text": "버스를 타지 않으시는 거죠? 자차로 오시나요, 아니면 불참인가요?", "pending": {"intent": "change"}}
            return {"type": "ask", "text": "어떤 항목으로 바꿀까요? 버스 / 자차 / 불참 / 미정 중에서 알려 주세요.", "pending": {"intent": "change"}}
        if FEE_WORDS.search(msg) or STATUS_WORDS.search(msg):
            return {"type": "tool", "name": "get_my_status", "args": {}}
        return {
            "type": "reply",
            "text": "참석 응답 변경이나 현재 응답·회비 확인을 도와드려요. 예) '버스 말고 자차로 갈게요', '내 회비 얼마예요?'",
        }

    # 팀 담당자: 팀 현황 / 확인 과제 답변 / 정정 요청
    def _manager(self, inp: AgentInput) -> dict[str, Any]:
        msg = inp.message
        steps = inp.steps
        option = "no" if NEGATIVE.search(msg) else ("yes" if POSITIVE.search(msg) else None)
        if steps:
            last = steps[-1]
            if not last["ok"]:
                return {"type": "reply", "text": _error_reply(last["error"])}
            if last["tool"] == "list_open_tasks":
                tasks = last["result"]["tasks"]
                if option and not CORRECTION_WORDS.search(msg):
                    if len(tasks) == 1:
                        return {"type": "tool", "name": "answer_task", "args": {"task_id": tasks[0]["task_id"], "option": option}}
                    if len(tasks) > 1:
                        listing = "\n".join(f"#{t['task_id']} {t['question']}" for t in tasks)
                        return {"type": "ask", "text": f"어느 확인 요청에 대한 답인가요? 번호로 알려 주세요.\n{listing}", "pending": {"intent": "answer_task", "option": option}}
                if not tasks:
                    return {"type": "reply", "text": "지금 답해야 할 확인 요청은 없어요."}
                listing = "\n".join(f"#{t['task_id']} {t['question']}" for t in tasks)
                return {"type": "reply", "text": f"확인 요청 {len(tasks)}건이 있어요.\n{listing}\n'#번호 네' 또는 '#번호 아니요'처럼 답해 주세요."}
            if last["tool"] == "get_team_summary":
                s = last["result"]
                c = s["counts"]
                text = (
                    f"{s['team_name']} 응답 현황: 버스 {c['bus']} · 자차 {c['car']} · 불참 {c['absent']} · 미정 {c['undecided']} · 미응답 {c['none']}\n"
                    f"최종 회비 {_won(s['final_fee'])}, 확정 수납 {_won(s['received'])}, 지급 환불 {_won(s['refunds_paid'])} → 잔액 {_won(abs(s['balance']))} ({s['balance_label']})"
                )
                if s["blockers"]:
                    text += "\n정산 확정 전 확인할 것: " + "; ".join(s["blockers"])
                return {"type": "reply", "text": text}
            return {"type": "reply", "text": "처리했어요."}

        m = re.search(r"#\s*(\d+)|(\d+)\s*번", msg)
        task_id = int(m.group(1) or m.group(2)) if m else None
        if inp.state == "awaiting_info" and (inp.pending or {}).get("intent") == "answer_task":
            if task_id:
                return {"type": "tool", "name": "answer_task", "args": {"task_id": task_id, "option": option or inp.pending.get("option", "yes")}}
            return {"type": "ask", "text": "확인 요청 번호(#숫자)를 알려 주세요.", "pending": inp.pending}
        if CORRECTION_WORDS.search(msg):
            return {"type": "tool", "name": "request_correction", "args": {"text": msg.strip()[:500]}}
        if task_id and option:
            return {"type": "tool", "name": "answer_task", "args": {"task_id": task_id, "option": option}}
        if option or TASK_WORDS.search(msg):
            return {"type": "tool", "name": "list_open_tasks", "args": {}}
        if SUMMARY_WORDS.search(msg):
            return {"type": "tool", "name": "get_team_summary", "args": {}}
        return {"type": "reply", "text": "팀 응답·청구 현황 확인, 확인 요청 답변, 명세 정정 요청을 도와드려요. 예) '우리 팀 정산 현황', '확인 요청 보여줘'"}


# ------------------------------------------------------------------ 행동 형식 검증

ACTION_TYPES = ("tool", "ask", "reply")


def validate_action(action: Any, tool_names: set[str]) -> dict[str, Any]:
    """LLM 출력이 행동 계약을 지키는지 검사. 어기면 LLMError → 실행하지 않고 규칙 기반으로 대체한다.
    도구 '인자'의 세부 검사(필수값·enum·권한)는 도구 실행 단계(run_tool)가 맡는다."""
    if not isinstance(action, dict) or action.get("type") not in ACTION_TYPES:
        raise LLMError(f"행동 형식 오류: {str(action)[:120]}")
    if action["type"] == "tool":
        if action.get("name") not in tool_names:
            raise LLMError(f"없는 도구 선택: {str(action.get('name'))[:60]}")
        if not isinstance(action.get("args", {}), dict):
            raise LLMError("도구 인자가 객체가 아님")
        return {"type": "tool", "name": action["name"], "args": action.get("args") or {}}
    text = action.get("text")
    if not isinstance(text, str) or not text.strip():
        raise LLMError(f"{action['type']} 에 text 가 없음")
    out = {"type": action["type"], "text": text.strip()[:1000]}
    if action["type"] == "ask":
        out["pending"] = action.get("pending") if isinstance(action.get("pending"), dict) else {}
    return out


# ------------------------------------------------------------------ Gemini (실제 연동)

SYSTEM_PROMPT = """너는 회사 워크숍(대모임) 총무를 돕는 '소통 Agent'다. 지금 대화 상대는 {role} {name}님이다.
너의 일: 요청 해석 → 필요한 기록 조회 도구 선택 → 정보가 부족하면 짧게 질문 → 갱신 도구 호출 → 결과 안내.

규칙
- 금액·환불액은 절대 직접 계산하거나 지어내지 않는다. 도구 결과(tool_results_this_turn)의 숫자만 인용한다.
- 본인(담당자라면 자기 팀) 데이터만 다룬다. 다른 사람의 응답 변경 요청은 target 에 그 사람 이름을 넣어 preview_change 로 확인하면 시스템이 거절한다.
- 갱신 도구(mutating=true)는 호출만 한다. 반영 전 사용자 확인 질문과 '네/아니요' 판정은 시스템 코드가 한다. 너는 확인 문구를 만들지 않는다.
- 같은 조회 도구를 같은 인자로 두 번 부르지 않는다. 결과가 이미 있으면 그것으로 답한다.
- 선택지(choice)는 bus(참석+버스), car(참석+자차), absent(불참), undecided(미정) 중 하나다.
  "버스 안 탈래요"처럼 자차인지 불참인지 모호하면 추측하지 말고 type=ask 로 물어본다.
- 질문할 때 pending 에 지금까지 파악한 의도를 넣는다. 예: {{"intent":"change"}}.
- conversation_state 가 awaiting_info 이면 pending 의 의도에 이어서 user_message 를 해석한다(대화 재개).
- 담당자의 확인 요청 답변은 list_open_tasks 로 과제 번호를 확인한 뒤 answer_task(task_id, option) 로 한다. 어떤 과제인지 모호하면 묻는다.

출력: JSON 객체 하나만.
  {{"type":"tool","name":"<도구 이름>","args":{{...}}}}
  {{"type":"ask","text":"<질문>","pending":{{"intent":"change"}}}}
  {{"type":"reply","text":"<답변>"}}
"""


class GeminiLLM:
    mode = "gemini"

    def __init__(self) -> None:
        self.last_model = ""

    def decide(self, inp: AgentInput) -> dict[str, Any]:
        prompt = {
            "tools": inp.tools,
            "conversation_state": inp.state,
            "pending": inp.pending,
            "recent_history": inp.history[-6:],
            "tool_results_this_turn": inp.steps,
            "user_message": inp.message,
        }
        role = "참가자" if inp.role == "participant" else "팀 담당자"
        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT.format(role=role, name=inp.actor_name)}]},
            "contents": [{"role": "user", "parts": [{"text": json.dumps(prompt, ensure_ascii=False)}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1},
        }
        last_err = ""
        for model in settings.gemini_models:
            try:
                r = httpx.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": settings.gemini_api_key},  # 키를 URL 에 넣지 않음
                    json=body,
                    timeout=30,
                )
            except httpx.HTTPError as e:
                last_err = f"{model}: {type(e).__name__}"
                continue
            if r.status_code in (404, 429, 500, 503):  # 계정 미지원·한도 소진·서버 오류 → 다음 모델
                last_err = f"{model}: HTTP {r.status_code}"
                continue
            if r.status_code != 200:
                raise LLMError(f"{model}: HTTP {r.status_code}")
            try:
                parts = r.json()["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                action = json.loads(text[text.find("{"): text.rfind("}") + 1])
            except (KeyError, IndexError, ValueError, TypeError) as e:
                raise LLMError(f"{model}: 응답 형식 오류 {type(e).__name__}") from None
            self.last_model = model
            return action
        raise LLMError(f"사용 가능한 Gemini 모델 없음 ({last_err})")


def get_llm() -> LLM:
    return GeminiLLM() if settings.llm_mode == "gemini" else MockLLM()
