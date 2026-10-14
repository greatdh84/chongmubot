"""총무 Agent 답변 카드: '비서가 총무에게 보고하는' 구조.

{type, greeting, headline, status{key,label}, story[{at, text, quote?, who?}], money{billed, final, diff, received?, balance?},
 rows[]?(여러 부서·사람), first?(가장 먼저 볼 것), todo[{label, action, target_id?, team?, kind?}], sources{slack, changes, receipts, team_id}}

- 숫자는 전부 코드(정산 보고서·근거 서비스)에서 온다. 템플릿(mock)이 기본이고, 실제 LLM 은 greeting·headline·story 문장만 바꿀 수 있으며
  그 문장에 도구 결과에 없는 금액이 나오면 버리고 템플릿을 쓴다(check_llm_card).
- 금액이 아직 확정 전(변경 승인 대기·입금 대조 전)이면 headline 에 분명히 쓰고, 거래내역 전에는 입금·잔액 줄을 숨긴다.
- todo.action: approve_change · accept_suggestion · confirm_allocation(화면에서 확인 후 실행) / open_changes · open_task · open_payments ·
  open_roster · open_evidence(이동) / notify_preview(채팅에서 '네' 확인 흐름).
"""
from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import clock
from ..models import BankTransaction, ChangeLog, ConfirmTask, DbMeta, Event, Team
from ..services import evidence, report as rpt

STATUS = {"extra": "추가 납부", "refund": "환불", "check": "확인 필요", "done": "완료"}


def won(v: int | None) -> str:
    return "미확정" if v is None else f"{v:,}원"


def signed(v: int | None) -> str:
    return "" if v is None else "0원" if v == 0 else f"{'+' if v > 0 else '−'}{abs(v):,}원"


def hm(iso: str | None) -> str:
    return (iso or "")[5:16].replace("-", "/").replace("T", " ")


def has_transactions(db: Session, event: Event) -> bool:
    return bool(db.scalar(select(func.count()).select_from(BankTransaction).where(BankTransaction.event_id == event.id)))


def _why(c: dict) -> str:
    """변동 이유를 짧게: 당일 참석·불참·자차 전환 …"""
    f, t, day = c["from"], c["to"], "당일 " if c.get("on_event_day") else ""
    if t == "불참":
        return f"{day}불참"
    if f in ("불참", "미응답", "미정") and t.startswith("참석"):
        return f"{day}참석" if f == "불참" else "늦은 응답(참석)"
    if f.endswith("버스") and t.endswith("자차"):
        return f"{day}자차 전환"
    if f.endswith("자차") and t.endswith("버스"):
        return f"{day}버스 전환"
    return f"{f}→{t}"


def _ro(word: str) -> str:
    """'불참으로' / '버스 전환으로' / '자차로' — 받침(ㄹ 제외) 있으면 '으로'."""
    ch = word[-1:]
    if "가" <= ch <= "힣":
        jong = (ord(ch) - 0xAC00) % 28
        return word + ("으로" if jong and jong != 8 else "로")
    return word + "(으)로"


def _pending_delta(c: dict) -> int | None:
    return None if c["basis_kind"] == "pending" else c.get("pending_delta")


# ---------------------------------------------------------------- 한 부서

def team_card(db: Session, event: Event, rep: Any, team_id: int, greeting: str | None = None) -> dict[str, Any]:
    t = next(x for x in rep.teams if x["team_id"] == team_id)
    e = evidence.team_evidence(db, event, db.get(Team, team_id))
    txns = has_transactions(db, event)
    mgr = e["team"]["manager"]
    b = e["billing"]
    changes = e["changes"]
    pending = [c for c in changes if c["status"] == "pending_approval"]
    applied = [c for c in changes if c["status"] == "applied"]
    diff = (e["final_fee"] - b["billed"]) if b else None

    # 상태 칩: 거래내역 전에는 잔액(=최종 회비 전액)이 '추가 납부'로 보이는 오해를 막고 변동만 본다
    if pending or (txns and t["status"] == "check"):
        status = "check"
    elif not txns:
        status = "extra" if (diff or 0) > 0 else "refund" if (diff or 0) < 0 else "done"
    else:
        status = t["status"]

    # 결론 한 문장
    tasks = [x for x in e["blockers"] if x.get("task_id")]
    sug = next((r for r in e["receipts"] if r.get("slack_suggestion")), None)
    props = [r for r in e["receipts"] if r["status"] == "proposed"]
    if pending:
        c = pending[0]
        d = _pending_delta(c)
        if d is None:
            head = f"{c['name']}님 {_why(c)}: 환불 규칙이 없어 총무님이 환불액을 정해야 해요 (승인 전)"
        else:
            head = f"{c['name']}님 {_ro(_why(c))} {abs(d):,}원 {'추가 납부가' if d > 0 else '환불이'} 필요해요 (총무님 승인 전)"
        if len(pending) > 1:
            head += f" · 승인 대기 {len(pending)}건"
    elif sug:
        s = sug["slack_suggestion"]
        head = f"'{sug['depositor']}' {sug['amount']:,}원 입금이 {s['sender']}님 슬랙과 일치해요. {s['team']} 입금으로 지정해 주세요"
    elif props:
        r = props[0]
        head = f"{r['depositor']} {r['amount']:,}원 입금 배정 제안을 확정해 주세요 ({r['basis'][:40]})"
    elif any("응답 미확정" in x["text"] for x in e["blockers"]):
        names = ", ".join(b["unresolved_names"]) if b and b["unresolved_names"] else next(x["text"] for x in e["blockers"] if "응답 미확정" in x["text"]).split(": ", 1)[-1]
        head = f"{names}님이 끝까지 참석 응답을 안 했어요. 회비 미확정이라 총무님이 확인해야 해요"
    elif t["status"] == "check" and txns:
        head = e["blockers"][0]["text"] if e["blockers"] else "확인할 것이 남아 있어요"
    elif not txns:
        head = (f"마감 후 변동으로 최종 회비가 {signed(diff)} 바뀌었어요 (입금 대조 전)" if diff else "마감 후 변동이 없어요 (입금 대조 전)")
    elif t["status"] == "extra":
        lead = next((c for c in applied if (c["delta"] or 0) > 0), None)
        head = (f"{lead['name']}님 {_ro(_why(lead))} " if lead else "청구액 미납으로 ") + f"{t['balance']:,}원 추가 납부가 필요해요 — 담당자 {mgr}님께 안내하면 돼요"
    elif t["status"] == "refund":
        lead = next((c for c in applied if (c["delta"] or 0) < 0), None)
        head = (f"{lead['name']}님 {_ro(_why(lead))} " if lead else "초과 입금으로 ") + f"{-t['balance']:,}원 환불할 차례예요 — 담당자 {mgr}님께 돌려드리면 돼요"
    else:
        head = "정산 끝났어요 — " + (f"청구 {won(b['billed'])} → 최종 {won(e['final_fee'])}, 입금까지 맞아요" if b else "입금까지 맞아요")

    # 무슨 일이 있었나(시간순 2~4줄)
    story: list[dict[str, Any]] = []
    for c in sorted(changes, key=lambda c: c["requested_at"]):
        msg = c["messages"][0] if c["messages"] else None
        line = {"at": c["requested_at"], "text": f"{c['name']}님 {c['from']} → {c['to']}"}
        if msg:
            line |= {"who": f"{'담당자 ' if msg['sender'] == mgr else ''}{msg['sender']}님 슬랙", "quote": msg["text"]}
        else:
            line["who"] = c["source"]
        if c["status"] == "applied":
            line["text"] += f" · {signed(c['delta'])}" + (f" ({'총무 결정' if c['basis_kind'] == 'admin' else '규칙'}, 승인 {hm(c['decided_at'])})" if c["decided_at"] else "")
        else:
            d = _pending_delta(c)
            line["text"] += " · " + ("환불액 미정(규칙 없음)" if d is None else f"승인되면 {signed(d)}") + " · 승인 대기"
        story.append(line)
    for r in e["receipts"]:
        if r.get("late_label", "") and r["late_label"].startswith("추가 납부 완료"):
            story.append({"at": r["txn_at"], "text": f"{r['depositor']}님 {r['amount']:,}원 추가 입금 확인", "who": "통장"})
        if r.get("slack_suggestion"):
            s = r["slack_suggestion"]
            story.append({"at": s["sent_at"], "text": f"'{r['depositor']}' {r['amount']:,}원 입금자 단서", "who": f"{s['sender']}님 슬랙", "quote": s["text"]})
    story.sort(key=lambda x: x["at"] or "")
    story = story[-4:]
    if not story and t["blockers"]:
        story = [{"at": None, "text": x["text"]} for x in e["blockers"][:2]]

    money = {"billed": b["billed"] if b else None, "final": e["final_fee"], "diff": diff, "show_received": txns}
    pend_sum = [_pending_delta(c) for c in pending]
    if pending and all(x is not None for x in pend_sum):
        money["if_approved"] = e["final_fee"] + sum(pend_sum)  # 승인되면 최종 회비(코드 계산 값의 합)
    if txns:
        money |= {"received": e["received"], "balance": e["balance"]}

    todo: list[dict[str, Any]] = []
    for c in pending:
        if c["basis_kind"] == "pending":
            todo.append({"label": f"{c['name']}님 환불액 정하고 승인", "action": "open_changes", "target_id": c["change_id"]})
        else:
            todo.append({"label": f"{c['name']}님 변경 승인", "action": "approve_change", "target_id": c["change_id"]})
    if sug:
        todo.append({"label": f"입금자 지정 → {sug['slack_suggestion']['team']}", "action": "accept_suggestion", "target_id": sug["task_id"]})
    for r in props:
        todo.append({"label": f"{r['depositor']} {r['amount']:,}원 배정 확정", "action": "confirm_allocation", "target_id": r["allocation_id"]})
    for x in tasks:
        if not (sug and x["task_id"] == sug.get("task_id")):
            todo.append({"label": f"과제 #{x['task_id']} 보기", "action": "open_task", "target_id": x["task_id"]})
    if any("응답 미확정" in x["text"] for x in e["blockers"]):
        todo.append({"label": "명단에서 응답 입력", "action": "open_roster"})
    if txns and t["status"] in ("extra", "refund") and not t["blockers"]:
        todo.append({"label": f"담당자 {mgr}님께 {'추가 납부' if t['status'] == 'extra' else '환불'} 안내 미리보기", "action": "notify_preview", "kind": t["status"], "team": t["name"]})
    if not txns and not pending:
        todo.append({"label": "거래내역 올리기", "action": "open_payments"})
    return {
        "type": "team", "greeting": greeting or f"총무님, {t['name']} 정리해 드릴게요.", "headline": head,
        "status": {"key": status, "label": STATUS[status] if txns or status != "done" else "변동 없음"},
        "team": {"team_id": team_id, "name": t["name"], "code": t["code"], "manager": mgr},
        "story": story, "money": money, "todo": todo[:3],
        "sources": {"slack": len(e["messages"]), "changes": len(changes) + len(e["before_billing"]), "receipts": len(e["receipts"]), "team_id": team_id},
    }


# ---------------------------------------------------------------- 여러 부서

def _team_line(db: Session, event: Event, rep: Any, t: dict) -> tuple[str, int, dict]:
    """부서 한 줄 이유와 '먼저 볼' 우선순위(작을수록 먼저)."""
    c = team_card(db, event, rep, t["team_id"])
    head = c["headline"]
    pri = 9
    first_todo = c["todo"][0] if c["todo"] else None
    if first_todo:
        pri = {"accept_suggestion": 1, "approve_change": 2, "open_changes": 3, "confirm_allocation": 4, "open_task": 5, "open_roster": 6, "notify_preview": 7}.get(first_todo["action"], 8)
    return head, pri, c


def teams_card(db: Session, event: Event, rep: Any, status: str) -> dict[str, Any]:
    rows = [t for t in rep.teams if status == "all" or t["status"] == status]
    txns = has_transactions(db, event)
    lines = []
    for t in rows:
        head, pri, c = _team_line(db, event, rep, t)
        lines.append({"team_id": t["team_id"], "team": t["name"], "manager": (t["manager"] or {}).get("name"), "amount": abs(t["balance"]), "status": t["status"],
                      "reason": head.split(" — 담당자 ")[0], "pri": pri, "todo": c["todo"][:1]})
    label = STATUS.get(status, "전체")
    total = sum(x["amount"] for x in lines)
    # '가장 먼저 볼 것'은 확인 필요 목록에서만(추가·환불 목록은 담당자 안내가 다음 할 일)
    first = min(lines, key=lambda x: (x["pri"], -x["amount"])) if lines and status in ("check", "all") else None
    if not lines:
        head = f"{label} 부서는 없어요."
    elif status == "check":
        head = f"확인 필요 {len(lines)}곳 중 먼저 볼 곳은 {first['team']}이에요 — {first['reason']}"
    elif status == "refund":
        head = f"환불할 부서는 {len(lines)}곳, 합계 {total:,}원이에요. 담당자 안내는 미리보기 후 '네'로 보내요"
    elif status == "extra":
        head = f"추가 납부 부서는 {len(lines)}곳, 합계 {total:,}원이에요" + ("" if txns else " (입금 대조 전이라 아직 최종 아님)")
    else:
        head = f"{label} {len(lines)}곳이에요"
    todo = list(first["todo"]) if first and first["todo"] else []
    if status in ("refund", "extra") and lines and txns:
        todo.append({"label": f"{label} 부서 {len(lines)}곳 담당자 안내 미리보기", "action": "notify_preview", "kind": status})
    for x in lines:
        x.pop("pri")
    return {
        "type": "teams", "greeting": f"총무님, {label} 부서 정리했어요." if status != "all" else "총무님, 부서별로 정리했어요.",
        "headline": head, "status": {"key": status, "label": label} if status in STATUS else None,
        "first": {"team_id": first["team_id"], "team": first["team"], "reason": first["reason"]} if first else None,
        "rows": lines, "story": [], "money": {"total": total} if status in ("extra", "refund") else None, "todo": todo[:3],
        "sources": {"teams": len(lines)},
    }


def summary_card(db: Session, event: Event, rep: Any) -> dict[str, Any]:
    s = rep.summary
    ts = s["team_status"]
    txns = has_transactions(db, event)
    b = briefing(db, event, rep, mark_new=False)
    return {
        "type": "summary", "greeting": f"총무님, 정산 보고서 v{rep.version} 전체 현황이에요.",
        "headline": (f"추가 납부 {ts['extra']} · 환불 {ts['refund']} · 확인 필요 {ts['check']} · 완료 {ts['done']} — " if txns else "아직 입금 대조 전이에요 — ")
        + (f"결정하실 일 {b['decisions']}건, 가장 급한 건 {b['urgent']['label']}" if b["urgent"] else "결정하실 일은 없어요"),
        "status": None, "story": [], "todo": [b["urgent"]] if b["urgent"] else [],
        "money": {"billed": s["billed_total"], "final": s["final_total"], "diff": s.get("billed_final_delta"), "show_received": txns, "received": s["received_total"] if txns else None},
        "rows": [], "sources": {"report_version": rep.version},
    }


def people_card(rep: Any, result: dict) -> dict[str, Any]:
    rows = [{"name": p["name"], "team": p["team"], "kind": p["kind_label"], "amount": p["amount"], "line": (p["changes"] or ["응답 없음"])[-1]} for p in result["people"]]
    t = result.get("team")
    head = f"{result['kind_label']} {result['count']}명이에요" + (f" ({t['name']})" if t else "")
    todo = []
    if t and result["kind"] in ("extra", "refund"):
        head += f" — 회비는 팀 단위라 {'추가 납부' if result['kind'] == 'extra' else '환불'}는 담당자 {t['manager'] or '(미지정)'}님께 안내해요"
        if t["status"] == result["kind"] and not t["blockers"]:
            todo.append({"label": f"담당자 {t['manager']}님께 안내 미리보기", "action": "notify_preview", "kind": t["status"], "team": t["name"]})
    return {"type": "people", "greeting": "총무님, 해당하는 분들이에요.", "headline": head,
            "status": None, "rows": rows, "story": [], "money": None, "todo": todo, "sources": {"people": len(rows)}}


def person_card(db: Session, event: Event, rep: Any, participant_id: int) -> dict[str, Any]:
    from ..models import Participant

    p = db.get(Participant, participant_id)
    c = team_card(db, event, rep, p.team_id, greeting=f"총무님, {p.name}님 건 정리해 드릴게요.")
    mine = [x for x in c["story"] if x["text"].startswith(f"{p.name}님") or p.name in (x.get("quote") or "")]
    hit = next((x for x in rep.people if x["participant_id"] == p.id), None)
    if hit:
        c["headline"] = f"{p.name}님: {rpt.KIND_LABEL[hit['kind']]}" + (f" {hit['amount']:,}원" if hit["amount"] else "") + (f" — {hit.get('process')}" if hit.get("process") else "") + f" ({c['team']['name']} 담당자 {c['team']['manager']}님)"
    else:
        c["headline"] = f"{p.name}님은 마감 후 변동이 없어요 ({c['team']['name']})"
    c["story"] = mine or c["story"][:2]
    c["type"] = "person"
    return c


def changed_card(result: dict) -> dict[str, Any]:
    if result.get("first"):
        return {"type": "summary", "greeting": "총무님,", "headline": f"보고서 v{result['report_version']} 이 첫 버전이라 비교할 이전 버전이 없어요.", "status": None, "story": [], "todo": [], "money": None, "rows": [], "sources": {}}
    rows = [{"team": t["name"], "reason": f"{signed(t['before']['balance']) if t['before'] else '새 부서'} → {signed(t['after']['balance'])} {STATUS.get(t['after']['status'], '')}"} for t in result.get("teams", [])]
    return {"type": "teams", "greeting": "총무님, 지난 버전 이후 바뀐 것이에요.", "headline": f"보고서 v{result['report_version']} — 바뀐 부서 {len(rows)}곳 (계기: {result.get('trigger')})",
            "status": None, "rows": rows, "story": [], "todo": [], "money": None, "sources": {}}


def confirm_card(dry: dict) -> dict[str, Any]:
    rows = [{"team": x["team"], "manager": x["manager"], "amount": x["amount"], "reason": "담당자에게만 DM"} for x in dry["targets"]]
    return {"type": "confirm", "greeting": "총무님, 보내기 전에 확인해 주세요.",
            "headline": f"{dry['kind_label']}를 {len(rows)}개 부서 담당자에게 Slack DM 으로 보낼까요? 맞으면 '네'를 눌러 주세요",
            "status": None, "rows": rows, "story": [{"at": None, "text": f"확인 필요로 보류: {', '.join(dry['skipped_check'])}"}] if dry["skipped_check"] else [],
            "money": {"total": sum(x["amount"] for x in rows)}, "todo": [], "sources": {}}


def result_card(r: dict) -> dict[str, Any]:
    d = r["dispatch"]
    mode = "모의 발송(개발용 수신함)" if d.get("notify_mode") == "mock" else "Slack DM"
    return {"type": "result", "greeting": "총무님, 보냈어요.",
            "headline": f"{r['kind_label']} {d.get('sent', 0)}건을 {mode}으로 담당자에게 보냈어요"
            + (f" · 연기 {d['deferred']}건(21~8시 금지·하루 한도 → 허용 시각에 자동 발송)" if d.get("deferred") else "")
            + (f" · 실패 {d['failed']}건(기록 탭에서 확인)" if d.get("failed") else "")
            + (" · 같은 금액으로 이미 보낸 부서는 다시 보내지 않았어요" if r["created"] < len(r["targets"]) else ""),
            "status": None, "rows": [{"team": x["team"], "manager": x["manager"], "amount": x["amount"]} for x in r["targets"]], "story": [],
            "money": None, "todo": [], "sources": {}}


# ---------------------------------------------------------------- 카드 → 글(대화 기록·정적 데모·LLM 이력)

def to_text(c: dict) -> str:
    out = [c.get("greeting") or "", c.get("headline") or ""]
    for s in c.get("story") or []:
        out.append(f"- {hm(s['at']) + ' ' if s.get('at') else ''}{s.get('who', '') + ' — ' if s.get('who') else ''}{s['text']}" + (f" “{s['quote']}”" if s.get("quote") else ""))
    for r in c.get("rows") or []:
        out.append(f"- {r['name'] + ' (' + r['team'] + ')' if r.get('name') else r.get('team')}{' · ' + r['manager'] if r.get('manager') else ''}{' · ' + won(r['amount']) if r.get('amount') else ''}{' · ' + r['reason'] if r.get('reason') else ''}{' · ' + r['line'] if r.get('line') else ''}")
    m = c.get("money") or {}
    if m.get("billed") is not None:
        out.append(f"청구 {won(m['billed'])} → 최종 {won(m['final'])}" + (f" ({signed(m['diff'])})" if m.get("diff") else ""))
    if c.get("todo"):
        out.append("하실 일: " + " / ".join(t["label"] for t in c["todo"]))
    return "\n".join(x for x in out if x)


# ---------------------------------------------------------------- LLM 문장 검증

AMOUNT = re.compile(r"\d{1,3}(?:,\d{3})+|\d{4,}")


def _numbers(obj: Any, out: set[int]) -> set[int]:
    if isinstance(obj, bool):
        return out
    if isinstance(obj, int):
        out.add(abs(obj))
    elif isinstance(obj, str):
        out.update(int(x.replace(",", "")) for x in AMOUNT.findall(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _numbers(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _numbers(v, out)
    return out


def check_llm_card(llm_card: Any, template: dict, steps: list[dict]) -> tuple[dict, str]:
    """LLM 이 쓴 greeting·headline·story 를 템플릿에 덮되, 도구 결과·템플릿에 없는 금액이 하나라도 있으면 버린다."""
    if not isinstance(llm_card, dict):
        return template, "LLM 카드 없음 → 템플릿"
    allowed = _numbers([s.get("result") for s in steps], set()) | _numbers(template, set())
    texts = [llm_card.get("greeting"), llm_card.get("headline"), *[(x.get("text") if isinstance(x, dict) else x) for x in (llm_card.get("story") or [])]]
    texts = [str(x) for x in texts if x]
    bad = sorted(_numbers(texts, set()) - allowed)
    if bad or not llm_card.get("headline"):
        return template, f"LLM 문장에 도구 결과에 없는 금액({', '.join(f'{x:,}' for x in bad[:3])}) → 템플릿" if bad else "LLM headline 없음 → 템플릿"
    out = dict(template)
    out["greeting"] = str(llm_card.get("greeting") or template["greeting"])[:120]
    out["headline"] = str(llm_card["headline"])[:200]
    if isinstance(llm_card.get("story"), list) and llm_card["story"]:
        out["story"] = [x if isinstance(x, dict) and x.get("text") else {"at": None, "text": str(x)[:200]} for x in llm_card["story"][:4]]
    out["by_llm"] = True
    return out, ""


# ---------------------------------------------------------------- 브리핑(대시보드를 열면 Agent 가 먼저)

def _seen_key(event: Event) -> str:
    return f"briefing_seen:{event.id}"


def briefing(db: Session, event: Event, rep: Any, mark_new: bool = True) -> dict[str, Any]:
    from ..models import Allocation, EvidenceMessage

    seen_row = db.get(DbMeta, _seen_key(event))
    seen = clock.parse_input(seen_row.value) if seen_row and seen_row.value else None
    msgs = list(db.scalars(select(EvidenceMessage).where(EvidenceMessage.event_id == event.id)))
    new_msgs = [m for m in msgs if seen is None or m.created_at > seen]
    pend = list(db.scalars(select(ChangeLog).where(ChangeLog.event_id == event.id, ChangeLog.status == "pending_approval").order_by(ChangeLog.requested_at)))
    proposals = [c for c in pend if c.source == "slack"]
    tasks = list(db.scalars(select(ConfirmTask).where(ConfirmTask.event_id == event.id, ConfirmTask.kind != "change_approval", ConfirmTask.status.in_(("open", "answered", "escalated")))))
    sug = [t for t in tasks if (t.evidence or {}).get("slack_suggestion")]
    props = list(db.scalars(select(Allocation).where(Allocation.event_id == event.id, Allocation.status == "proposed")))
    card = evidence.settlement_card(db, event)
    from ..models import Participant

    people = {p.id: p for p in db.scalars(select(Participant).where(Participant.event_id == event.id))}
    names = {k: p.name for k, p in people.items()}
    teams = {t.id: t.name for t in db.scalars(select(Team).where(Team.event_id == event.id))}
    team_by_name = {v: k for k, v in teams.items()}

    # 가장 급한 것: 근거가 준비돼 바로 정할 수 있는 결정부터(규칙 금액 승인 → 슬랙 총무 결정이 있는 환불액 → 슬랙 입금자 근거 → 배정 제안 → 근거 없는 판단 → 과제)
    ruleless = [c for c in pend if c.rule_missing]
    suggested = {m.change_id: (m.analysis or {}).get("suggested_refund") for m in msgs if (m.analysis or {}).get("outcome") == "suggest_refund"}
    sug.sort(key=lambda t: -(db.get(BankTransaction, t.transaction_id).amount if t.transaction_id else 0))
    cands: list[dict[str, Any]] = []
    for c in pend:
        lab = f"{names.get(c.participant_id)}님 {rpt.CHOICE_LABELS[c.from_choice]}→{rpt.CHOICE_LABELS[c.to_choice]} "
        if not c.rule_missing:
            cands.append({"pri": 1, "label": lab + f"승인({signed(c.extra_charge - c.refund_amount)})", "action": "approve_change", "target_id": c.id, "team_id": people[c.participant_id].team_id})
        elif suggested.get(c.id) is not None:
            cands.append({"pri": 2, "label": lab + f"환불액 {suggested[c.id]:,}원(슬랙 총무 결정) 넣고 승인", "action": "open_changes", "target_id": c.id, "team_id": people[c.participant_id].team_id})
        else:
            cands.append({"pri": 5, "label": lab + "환불액 정하고 승인", "action": "open_changes", "target_id": c.id, "team_id": people[c.participant_id].team_id})
    for t in sug:
        cands.append({"pri": 3, "label": f"'{t.evidence.get('depositor')}' 입금 → {t.evidence['slack_suggestion']['team']} 지정", "action": "accept_suggestion", "target_id": t.id, "team_id": t.team_id or team_by_name.get(t.evidence["slack_suggestion"]["team"])})
    for a in props:
        cands.append({"pri": 4, "label": f"{teams.get(a.team_id)} 입금 배정 제안 확정", "action": "confirm_allocation", "target_id": a.id, "team_id": a.team_id})
    for t in tasks:
        if t not in sug:
            cands.append({"pri": 6, "label": f"과제 #{t.id} 보기", "action": "open_task", "target_id": t.id, "team_id": t.team_id})
    urgent = min(cands, key=lambda x: x["pri"]) if cands else None
    if urgent:
        urgent = {k: v for k, v in urgent.items() if k != "pri"}
    decisions = len(pend) + len(sug) + len(props) + len([t for t in tasks if t.status == "escalated" and t not in sug])
    items = []
    if msgs:
        kinds: dict[str, int] = {}
        for m in msgs:
            k = (m.analysis or {}).get("kind")
            kinds[k] = kinds.get(k, 0) + 1
        items.append({"icon": "💬", "text": f"슬랙 {len(msgs)}건 읽음" + (f"(새로 {len(new_msgs)}건)" if seen and new_msgs else "") + f" — 참석 변경 {kinds.get('change', 0)} · 입금 단서 {kinds.get('deposit_hint', 0)} · 총무 결정 {kinds.get('admin_refund', 0)}"})
    if pend:
        items.append({"icon": "📝", "text": f"변경 승인 대기 {len(pend)}건" + (f"(슬랙에서 찾은 제안 {len(proposals)}건)" if proposals else "") + (f" · 환불액을 정해야 하는 건 {len(ruleless)}건" if ruleless else "")})
    if card["total"]:
        items.append({"icon": "🏦", "text": f"입금 대조: 거래 {card['total']}건 → 자동 확정 {card['auto']}건({card['rate']}%) · 사람 확인 {card['human']}건" + (f"({', '.join(f'{k} {v}' for k, v in card['reasons'].items())})" if card["reasons"] else "")})
    else:
        items.append({"icon": "🏦", "text": "입금 대조 전 — 거래내역을 올리면 정산 Agent 가 맞춰 볼게요"})
    ts = rep.summary["team_status"]
    if card["total"]:
        items.append({"icon": "📊", "text": f"보고서 v{rep.version}: 추가 납부 {ts['extra']} · 환불 {ts['refund']} · 확인 필요 {ts['check']} · 완료 {ts['done']}"})
    return {
        "type": "briefing", "greeting": "총무님, " + ("지난번 이후 이런 일이 있었어요." if seen else "지금까지 이런 일이 있었어요."),
        "headline": (f"결정하실 일 {decisions}건 — 가장 급한 건 {urgent['label']}" if urgent else "지금 결정하실 일은 없어요"),
        "items": items, "decisions": decisions, "urgent": urgent, "todo": [urgent] if urgent else [], "seen_at": clock.fmt(seen) if seen else None,
        "new_messages": len(new_msgs) if mark_new else None, "report_version": rep.version,
    }


def mark_seen(db: Session, event: Event) -> None:
    db.merge(DbMeta(key=_seen_key(event), value=clock.fmt(clock.now())))


# ---------------------------------------------------------------- 총무 행동 뒤 결과 보고

def after_action(db: Session, event: Event, rep: Any, *, kind: str, team_id: int | None, what: str) -> dict[str, Any]:
    """승인·지정·확정 뒤 Agent 가 짧게 보고: 무엇이 바뀌었고 다음에 무엇을 하면 되는지."""
    txns = has_transactions(db, event)
    t = next((x for x in rep.teams if x["team_id"] == team_id), None) if team_id else None
    todo: list[dict[str, Any]] = []
    head = what
    if t:
        head += f" {t['name']} 최종 회비가 {t['final_fee']:,}원이 됐고 보고서 v{rep.version}에 반영했어요."
        mgr = (t["manager"] or {}).get("name")
        if not txns:
            head += " 거래내역을 올리면 입금과 맞춰 볼게요."
        elif t["status"] in ("extra", "refund") and not t["blockers"]:
            head += f" {mgr}님께 {'추가 납부' if t['status'] == 'extra' else '환불'} 안내를 보낼까요?"
            todo.append({"label": "안내 미리보기", "action": "notify_preview", "kind": t["status"], "team": t["name"]})
        elif t["status"] == "check":
            head += f" 아직 확인할 것이 있어요: {t['blockers'][0]}"
            todo.append({"label": f"{t['name']} 근거 보기", "action": "open_evidence", "target_id": t["team_id"]})
        else:
            head += " 이 부서는 정산이 끝났어요."
    else:
        head += f" 보고서 v{rep.version}에 반영했어요."
    nxt = briefing(db, event, rep, mark_new=False)["urgent"]
    if nxt and not todo:
        todo.append(nxt | {"label": "다음: " + nxt["label"]})
    return {"type": "result", "greeting": "총무님, 처리했어요.", "headline": head, "status": {"key": t["status"], "label": STATUS[t["status"]]} if t and txns else None,
            "story": [], "money": None, "rows": [], "todo": todo[:2], "sources": {"team_id": team_id} if team_id else {}, "kind": kind}


def dumps(c: dict) -> str:
    return json.dumps(c, ensure_ascii=False)
