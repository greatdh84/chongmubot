"""LLM 어댑터. LLM 은 '다음 행동'만 고르고(도구 호출 / 추가 질문 / 답변), 금액 계산·권한·기록은 코드가 한다.

행동(Action) 계약 — LLM 출력은 반드시 아래 JSON 중 하나:
  {"type": "tool",  "name": "<도구 이름>", "args": {...}}
  {"type": "ask",   "text": "<사용자에게 물을 말>", "pending": {"intent": "...", ...}}
  {"type": "reply", "text": "<최종 답변>"}

LLM_MODE=mock  : 규칙 기반 MockLLM (외부 키 불필요, 결정적)
LLM_MODE=claude: Claude(소모임과 같은 LLM_API_KEY, 기본 claude-haiku-5-5) 호출
LLM_MODE=gemini: Gemini REST 호출
실제 LLM 이 실패하거나 형식이 틀리면 MockLLM 으로 대체하고 그 사실을 실행 기록에 남긴다.
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
    role: str  # participant | manager | admin
    message: str
    state: str
    pending: dict[str, Any] | None
    steps: list[dict[str, Any]]  # 이번 턴에 실행한 도구 [{tool, args, ok, result|error}]
    tools: list[dict[str, Any]]  # 사용할 수 있는 도구 계약
    history: list[dict[str, str]] = field(default_factory=list)
    actor_name: str = ""
    context: dict[str, Any] = field(default_factory=dict)  # 총무 Agent: 부서 목록·보고서 단계


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
        if inp.role == "admin":
            return admin_mock(inp)
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


# ------------------------------------------------------------------ 총무 Agent 규칙 기반 해석

A_NOTIFY = re.compile(r"알림|안내|연락|보내|발송|DM|디엠|메시지", re.I)
A_EXTRA = re.compile(r"추가 ?(회비|납부|금|로|요금|비용)?|더 ?내|덜 ?낸|더 ?걷|미납|부족")
A_REFUND = re.compile(r"환불|돌려|반환|초과 ?납부|더 ?낸 ?팀")
A_CHECK = re.compile(r"확인 ?필요|확인할|막힌|막혀|남은 ?일|문제|이상|걸리")
A_PENDING = re.compile(r"승인 ?대기|승인할")
A_UNRESOLVED = re.compile(r"미응답|미확정|응답 ?안|미정")
A_CHANGED = re.compile(r"지난 ?(보고서|버전)|새로|최근|달라진|바뀐 ?(것|점|부분)|업데이트|갱신")
A_DAY = re.compile(r"당일")
A_SUMMARY = re.compile(r"요약|현황|전체|보고서|정산|얼마|총액|합계|상태|어떻게")
A_PERSON = re.compile(r"([가-힣]{2,4})\s*(?:님|씨)")
A_LIST_WORDS = re.compile(r"누구|명단|목록|사람|대상|알려|보여")
A_WHY = re.compile(r"왜|이유|근거|어째서|어떻게 ?(나온|된|계산)")
A_STATUS = re.compile(r"현황|상태|어때|어떻게 ?돼")


def _a_team(inp: AgentInput) -> str | None:
    text = inp.message.replace(" ", "").upper()
    teams = inp.context.get("teams", [])
    for t in sorted(teams, key=lambda t: -len(t["name"])):
        name = t["name"].replace(" ", "").upper()
        if name in text or (len(name) > 2 and name.removesuffix("팀") in text):
            return t["name"]
    m = re.search(r"T\d{2}", text)
    if m:
        return m.group(0)
    # '박채원 팀 현황'처럼 담당자 이름 + 팀
    for t in teams:
        if t.get("manager") and re.search(re.escape(t["manager"]) + r"\s*(님|씨)?\s*(의\s*)?(팀|부서)", inp.message):
            return t["name"]
    return None


def _hm(iso: str | None) -> str:
    return (iso or "")[5:16].replace("-", "/").replace("T", " ")


def _was(word: str) -> str:
    """'불참이었는데' / '참석+버스였는데' — 받침 있으면 '이었는데'."""
    ch = word[-1:] if word else ""
    return word + ("이었는데" if "가" <= ch <= "힣" and (ord(ch) - 0xAC00) % 28 else "였는데")


def _signed(v: int | None) -> str:
    return "" if v is None else ("0원" if v == 0 else f"{'+' if v > 0 else '−'}{abs(v):,}원")


def _evidence_reply(r: dict[str, Any], dept: dict[str, Any] | None) -> str:
    """get_evidence 결과로 '왜'를 설명: 변동마다 누가·언제·무엇에서 무엇으로·근거(규칙/총무 결정·슬랙)·금액."""
    t, b = r["team"], r.get("billing")
    who = r.get("person")
    out = [f"{who['name']}님 ({t['name']} · 담당자 {t['manager'] or '미지정'})" if who else f"{t['name']}({t['code']}) · 담당자 {t['manager'] or '미지정'}" + (f" · {r['status_label']}" if r.get("status_label") else "")]
    if b:
        out.append(f"청구(납부 마감 {_hm(b['deadline'])} 청구표): {b['formula']}" + (f" · 청구 때 미확정 {', '.join(b['unresolved_names'])}" if b["unresolved_names"] else ""))
    for c in r["changes"]:
        msg = c["messages"][0] if c["messages"] else None
        when = f"{_hm(c['requested_at'])}{' 당일' if c['on_event_day'] else ''}"
        via = f"({msg['sender']}{' 담당자' if msg['sender'] == t['manager'] else ''} 슬랙 “{msg['text'][:40]}{'…' if len(msg['text']) > 40 else ''}”)" if msg else f"({c['source']})"
        amt = _signed(c["delta"]) if c["status"] == "applied" else (c["pending"] or "승인 대기")
        tail = f" · {'총무 결정' if c['basis_kind'] == 'admin' else '규칙'}: {c['basis']}" if c["status"] == "applied" else " · 총무 승인 대기"
        ok = f" · 승인 {_hm(c['decided_at'])}" if c["status"] == "applied" and c["decided_at"] else ""
        out.append(f"- {c['name']}님이 {_was(c['from'])} {when} {c['to']} {via} → {amt}{tail}{ok}")
    if not r["changes"]:
        out.append("- 마감 후 변동 없음")
    late = [x for x in r.get("receipts", []) if x.get("late")]
    if r.get("received") is not None:
        out.append(f"최종 회비 {_won(r['final_fee'])} · 확정 입금 {_won(r['received'])}" + (" · " + ", ".join(f"{x['late']} {x['amount']:,}원" for x in late) if late else ""))
    bal = r["balance"]
    out.append(f"→ 정산 잔액 {_signed(bal)}: " + ("담당자가 더 걷어 낼 돈(추가 납부)" if bal > 0 else "총무가 담당자에게 돌려줄 돈(환불)" if bal < 0 else "정산 완료"))
    if r["blockers"]:
        out.append("확인 필요: " + "; ".join(r["blockers"]))
    return "\n".join(out)


def _a_reply(inp: AgentInput) -> dict[str, Any]:
    last = inp.steps[-1]
    if not last["ok"]:
        return {"type": "reply", "text": last["error"]["message"]}
    r, tool = last["result"], last["tool"]
    if tool == "get_department" and A_STATUS.search(inp.message) and not any(s["tool"] == "get_evidence" for s in inp.steps):
        return {"type": "tool", "name": "get_evidence", "args": {"team": r["team"]["name"]}}
    if tool == "get_evidence":
        dept = next((s["result"] for s in inp.steps if s["tool"] == "get_department" and s["ok"]), None)
        return {"type": "reply", "text": _evidence_reply(r, dept)}
    if tool == "get_report_summary":
        ts, pc = r["team_status"], r["people_counts"]
        billed = f"청구 {_won(r['billed_total'])} → " if r["billed_total"] is not None else ""
        return {"type": "reply", "text": (
            f"정산 보고서 v{r['report_version']} ({r['stage']})\n"
            f"인원 {r['headcount']}명 중 참석 {r['attending']}명 · {r['teams']}개 부서\n"
            f"회비: {billed}최종 {_won(r['final_total'])}, 확정 입금 {_won(r['received_total'])}\n"
            f"추가 납부 {ts['extra']}개 부서 {_won(r['extra_total'])} · 환불 {ts['refund']}개 부서 {_won(r['refund_total'])} · 확인 필요 {ts['check']}개 부서 · 정산 완료 {ts['done']}개 부서\n"
            f"사람별: 추가 납부 {pc['extra']}명, 환불 {pc['refund']}명, 승인 대기 {pc['pending']}명, 환불액 미정 {pc['rule_missing']}명, 응답 미확정 {pc['unresolved']}명 (당일 변동 {r['on_event_day_changes']}명)"
        )}
    if tool in ("get_department", "list_people"):
        t = r.get("team")
        people = r["people"]
        head = []
        if t:
            gap = ""
            if t["payment_gap"]:
                gap = f" · 청구 대비 입금 {'부족' if t['payment_gap'] > 0 else '초과'} {_won(abs(t['payment_gap']))}"
            head.append(
                f"{t['name']}({t['code']}) · 담당자 {t['manager'] or '미지정'} · {t['attending']}/{t['headcount']}명 참석\n"
                f"청구 {_won(t['billed'])} → 최종 {_won(t['final_fee'])}, 확정 입금 {_won(t['received'])}{gap}\n"
                f"정산 잔액 {_won(abs(t['balance']))} ({t['status_label']})"
            )
            if t["blockers"]:
                head.append("확인할 것: " + "; ".join(t["blockers"]))
        label = r.get("kind_label", "변동")
        if tool == "list_people" and r["kind"] != "all":
            title = f"{label} 대상 {r['count']}명" + ("" if t else " (전체 부서)")
        else:
            title = f"변동이 있는 사람 {len(people)}명"
        rows = [f"- {p['name']}{'' if t else ' (' + p['team'] + ')'}: {p['kind_label']} {_won(p['amount']) if p['amount'] else ''} — {' / '.join(p['changes']) or '변경 기록 없음'}" for p in people]
        body = "\n".join([*head, title + (":" if rows else ""), *rows])
        if t and tool == "list_people" and r["kind"] in ("extra", "refund"):
            body += f"\n※ 회비는 팀 단위라 {'추가 납부는' if r['kind'] == 'extra' else '환불은'} 담당자 {t['manager'] or '(미지정)'}님에게 안내합니다. '{t['name']} 담당자에게 안내 보내줘'라고 하면 보낼게요."
        return {"type": "reply", "text": body}
    if tool == "list_teams":
        rows = [f"- {t['name']}({t['code']}) · 담당자 {t['manager'] or '미지정'} · {_won(abs(t['balance']))}" + (f" · {'; '.join(t['blockers'])}" if t["status"] == "check" else "") for t in r["teams"]]
        total = f", 합계 {_won(r['total'])}" if r["status"] in ("extra", "refund") else ""
        return {"type": "reply", "text": f"{r['status_label']} 부서 {r['count']}곳{total}" + (":\n" + "\n".join(rows) if rows else "")}
    if tool == "find_person":
        rows = []
        for p in r["people"]:
            x = f"- {p['name']} ({p['team']}): 현재 {LABEL[p['choice']]}, 회비 {_won(p['fee'])}"
            if p["in_report"]:
                ip = p["in_report"]
                x += f" · 보고서: {ip['kind_label']} {_won(ip['amount']) if ip['amount'] else ''} — {' / '.join(ip['changes'])}"
            rows.append(x)
        return {"type": "reply", "text": ("동명이인이 있어요.\n" if r["count"] > 1 else "") + "\n".join(rows)}
    if tool == "what_changed":
        if r.get("first"):
            return {"type": "reply", "text": f"보고서 v{r['report_version']} 이 첫 버전이라 비교할 이전 버전이 없어요."}
        rows = [f"- {t['name']}: {(_won(abs(t['before']['balance'])) + ' ' + t['before']['status']) if t['before'] else '새 부서'} → {_won(abs(t['after']['balance']))} {t['after']['status']}" for t in r["teams"]]
        rows += [f"+ {p['name']}({p['team']}) {p['kind']} {_won(p['amount'])}" for p in r["people_added"]]
        rows += [f"− {p['name']}({p['team']}) {p['kind']} 해소" for p in r["people_removed"]]
        return {"type": "reply", "text": f"보고서 v{r['report_version']}(계기: {r['trigger']}) 에서 바뀐 것:\n" + ("\n".join(rows) or "달라진 것이 없어요.")}
    return {"type": "reply", "text": "확인했어요."}


def admin_mock(inp: AgentInput) -> dict[str, Any]:
    if inp.steps:
        return _a_reply(inp)
    msg = inp.message
    team = _a_team(inp)
    args: dict[str, Any] = {"team": team} if team else {}
    refund, extra = bool(A_REFUND.search(msg)), bool(A_EXTRA.search(msg)) and not A_REFUND.search(msg)
    if A_NOTIFY.search(msg) and (refund or extra or "담당자" in msg):
        if not (refund or extra):
            return {"type": "ask", "text": "환불 안내와 추가 납부 안내 중 어떤 것을 보낼까요?", "pending": {"intent": "notify"}}
        return {"type": "tool", "name": "notify_managers", "args": {**args, "kind": "refund" if refund else "extra"}}
    if inp.state == "awaiting_info" and (inp.pending or {}).get("intent") == "notify" and (refund or extra):
        return {"type": "tool", "name": "notify_managers", "args": {**args, "kind": "refund" if refund else "extra"}}
    person = A_PERSON.search(msg + " ")
    if A_WHY.search(msg) and (team or person):
        if team:
            return {"type": "tool", "name": "get_evidence", "args": {"team": team}}
        if person.group(1) not in ("담당자", "팀장", "총무"):
            return {"type": "tool", "name": "get_evidence", "args": {"person": person.group(1)}}
    if person and not team and person.group(1) not in ("담당자", "팀장", "총무"):
        return {"type": "tool", "name": "find_person", "args": {"name": person.group(1)}}
    if A_CHANGED.search(msg) and not (refund or extra):
        return {"type": "tool", "name": "what_changed", "args": {}}
    day = bool(A_DAY.search(msg))
    kind = "extra" if extra else "refund" if refund else "pending" if A_PENDING.search(msg) else "unresolved" if A_UNRESOLVED.search(msg) else None
    if kind is None and A_CHECK.search(msg):
        if team:
            return {"type": "tool", "name": "get_department", "args": args}
        return {"type": "tool", "name": "list_teams", "args": {"status": "check"}}
    if kind or day:
        if team or A_LIST_WORDS.search(msg) and ("사람" in msg or "자" in msg or "명" in msg or day):
            return {"type": "tool", "name": "list_people", "args": {**args, "kind": kind or "all", **({"on_event_day": True} if day else {})}}
        if kind in ("extra", "refund"):
            return {"type": "tool", "name": "list_teams", "args": {"status": kind}}
        return {"type": "tool", "name": "list_people", "args": {"kind": kind}}
    if team:
        return {"type": "tool", "name": "get_department", "args": args}
    if A_SUMMARY.search(msg):
        return {"type": "tool", "name": "get_report_summary", "args": {}}
    return {"type": "reply", "text": "정산 보고서에 대해 물어봐 주세요. 예) '경영지원팀의 추가 회비 납부자를 알려줘', '환불할 부서 목록', '확인 필요한 부서', '당일 변동 있던 사람', '지난 보고서 이후 바뀐 것', '환불 대상 담당자에게 안내 보내줘'"}


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


ADMIN_PROMPT = """너는 회사 워크숍(대모임, 300명·부서별 팀) 총무를 돕는 '총무 Agent'다. 총무의 질문을 정산 보고서 조회로 바꿔 답한다.
보고서는 코드가 계산한다: 납부 마감 때 저장한 부서별 청구표 + 그 뒤의 변동 기록(마감 후 변경·당일 변동·입금 배정·환불 지급).
규칙
- 금액·인원은 절대 직접 계산하거나 지어내지 않는다. tool_results_this_turn 의 숫자만 인용한다.
- 부서 이름은 context.teams 에서 고른다. 모호하면 type=ask 로 묻는다.
- notify_managers 는 호출만 한다. 대상 확인 질문과 '네' 판정·발송은 시스템 코드가 한다.
- 회비는 팀 단위: 추가 납부·환불은 개인이 아니라 그 부서 팀 담당자에게 안내된다는 점을 답변에 반영한다.
- '왜', '이유', '근거'를 물으면 get_evidence 로 변동마다 누가·언제(일시)·무엇에서 무엇으로·근거(규칙/총무 결정·슬랙 메시지 보낸 사람)를 인용한다.
- '박채원 팀'처럼 사람 이름 + 팀이면 context.teams 의 manager 로 부서를 찾는다.
- 같은 조회를 같은 인자로 두 번 부르지 않는다. 결과가 있으면 한국어로 짧고 구체적으로(이름·금액·사유) 답한다.
출력: JSON 객체 하나만. {"type":"tool","name":...,"args":{...}} | {"type":"ask","text":...} | {"type":"reply","text":...}
"""


def _prompt_parts(inp: AgentInput) -> tuple[str, dict[str, Any]]:
    payload = {
        "tools": inp.tools,
        "conversation_state": inp.state,
        "pending": inp.pending,
        "recent_history": inp.history[-6:],
        "tool_results_this_turn": inp.steps,
        "user_message": inp.message,
    }
    if inp.role == "admin":
        return ADMIN_PROMPT, payload | {"context": inp.context}
    role = "참가자" if inp.role == "participant" else "팀 담당자"
    return SYSTEM_PROMPT.format(role=role, name=inp.actor_name), payload


def _parse_action(text: str) -> dict[str, Any]:
    try:
        return json.loads(text[text.find("{"): text.rfind("}") + 1])
    except (ValueError, TypeError) as e:
        raise LLMError(f"응답 형식 오류 {type(e).__name__}") from None


class ClaudeLLM:
    """Claude Messages API. 소모임(app/server.py)과 같은 키(LLM_API_KEY)·같은 모델 계열을 쓴다."""

    mode = "claude"

    def __init__(self) -> None:
        self.last_model = ""

    def decide(self, inp: AgentInput) -> dict[str, Any]:
        system, payload = _prompt_parts(inp)
        try:
            r = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": settings.claude_api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": settings.claude_model, "max_tokens": 800, "temperature": 0.1, "system": system,
                      "messages": [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]},
                timeout=30,
            )
        except httpx.HTTPError as e:
            raise LLMError(f"Claude 연결 오류: {type(e).__name__}") from None
        if r.status_code != 200:
            raise LLMError(f"Claude HTTP {r.status_code}")
        try:
            text = "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")
        except (KeyError, ValueError, TypeError) as e:
            raise LLMError(f"Claude 응답 형식 오류 {type(e).__name__}") from None
        self.last_model = settings.claude_model
        return _parse_action(text)


class GeminiLLM:
    mode = "gemini"

    def __init__(self) -> None:
        self.last_model = ""

    def decide(self, inp: AgentInput) -> dict[str, Any]:
        system, prompt = _prompt_parts(inp)
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
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


def complete_json(system: str, payload: dict[str, Any]) -> tuple[Any, str]:
    """행동 계약 밖의 '추출' 작업용(정산 Agent 가 슬랙 대화를 읽을 때). LLM_MODE 의 실제 LLM 에 JSON 하나를 받아
    (결과, 모델 이름)을 돌려준다. mock 이거나 실패하면 LLMError — 호출한 쪽이 규칙 기반으로 대체한다.
    결과는 반드시 호출한 쪽 코드가 명단·현재 응답과 대조 검증한 뒤에만 쓴다."""
    text_in = json.dumps(payload, ensure_ascii=False)
    if settings.llm_mode == "claude":
        if not settings.claude_api_key:
            raise LLMError("Claude 키 없음(LLM_API_KEY)")
        try:
            r = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": settings.claude_api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": settings.claude_model, "max_tokens": 2000, "temperature": 0, "system": system, "messages": [{"role": "user", "content": text_in}]},
                timeout=40,
            )
        except httpx.HTTPError as e:
            raise LLMError(f"Claude 연결 오류: {type(e).__name__}") from None
        if r.status_code != 200:
            raise LLMError(f"Claude HTTP {r.status_code}")
        try:
            text = "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")
        except (KeyError, ValueError, TypeError) as e:
            raise LLMError(f"Claude 응답 형식 오류 {type(e).__name__}") from None
        return _parse_action(text), settings.claude_model
    if settings.llm_mode == "gemini":
        if not settings.gemini_api_key:
            raise LLMError("Gemini 키 없음(GEMINI_API_KEY)")
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": text_in}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
        }
        last_err = ""
        for model in settings.gemini_models:
            try:
                r = httpx.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", headers={"x-goog-api-key": settings.gemini_api_key}, json=body, timeout=40)
            except httpx.HTTPError as e:
                last_err = f"{model}: {type(e).__name__}"
                continue
            if r.status_code in (404, 429, 500, 503):
                last_err = f"{model}: HTTP {r.status_code}"
                continue
            if r.status_code != 200:
                raise LLMError(f"{model}: HTTP {r.status_code}")
            try:
                parts = r.json()["candidates"][0]["content"]["parts"]
                text = "".join(x.get("text", "") for x in parts if not x.get("thought"))
            except (KeyError, IndexError, ValueError, TypeError) as e:
                raise LLMError(f"{model}: 응답 형식 오류 {type(e).__name__}") from None
            return _parse_action(text), model
        raise LLMError(f"사용 가능한 Gemini 모델 없음 ({last_err})")
    raise LLMError("LLM_MODE=mock")


def get_llm() -> LLM:
    if settings.llm_mode == "claude":
        return ClaudeLLM()
    return GeminiLLM() if settings.llm_mode == "gemini" else MockLLM()
