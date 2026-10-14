"""실제 LLM(Gemini) 소통 Agent 검증 — 합성 데이터, 임시 DB.

    GEMINI_API_KEY=... ../../.venv/bin/python scripts/llm_live_check.py

- 시연·운영 DB 를 건드리지 않도록 임시 폴더에 새 DB 를 만든다.
- 각 턴마다 '모델이 고른 행동(decisions)'과 '코드가 실행한 도구·결과(steps)'를 나눠 기록하고,
  DB 를 직접 조회해 기록이 바뀌었는지(또는 바뀌지 않았는지) 확인한다.
- 결과: var/llm_live/report-<시각>.json, .md (Git 제외)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import clock, db as dbmod  # noqa: E402
from app.config import BACKEND_DIR, settings  # noqa: E402

if not os.environ.get("GEMINI_API_KEY"):
    raise SystemExit("GEMINI_API_KEY 환경변수가 없습니다. (값은 출력하지 않습니다)")
settings.llm_mode = "gemini"
settings.gemini_api_key = os.environ["GEMINI_API_KEY"]
if os.environ.get("GEMINI_MODEL"):
    settings.gemini_models = [os.environ["GEMINI_MODEL"]] + [m for m in settings.gemini_models if m != os.environ["GEMINI_MODEL"]]

tmp = tempfile.mkdtemp(prefix="llm-live-")
dbmod.configure(f"sqlite:///{tmp}/live.db")
dbmod.init_db()

from app.agents import communication  # noqa: E402
from app.agents.settlement_agent import Run, ask_manager  # noqa: E402
from app.models import Admin, ChangeLog, ConfirmTask, Event, Participant, RefundRule, Response, Team, TeamManager  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.services import responses  # noqa: E402

NOW = datetime(2026, 10, 12, 2, 0)  # UTC = KST 11:00
db = dbmod.SessionLocal()


def build_world():
    clock.freeze(NOW - timedelta(days=10))
    admin = Admin(email="admin@example.com", password_hash=hash_password("x"))
    db.add(admin)
    db.flush()
    ev = Event(admin_id=admin.id, name="합성 워크숍", mode="large", event_at=NOW + timedelta(days=5),
               attend_deadline=NOW - timedelta(days=6), payment_deadline=NOW - timedelta(days=1), fee_bus=20000, fee_car=15000, fee_absent=0)
    db.add(ev)
    db.flush()
    db.add_all([
        RefundRule(event_id=ev.id, change_type="attend_to_absent", valid_until=ev.payment_deadline, refund_percent=100, description="입금 마감 전 불참 100%"),
        RefundRule(event_id=ev.id, change_type="attend_to_absent", valid_until=ev.event_at - timedelta(days=3), refund_percent=50, description="입금 마감 후 ~ 행사 3일 전 불참 50%"),
        RefundRule(event_id=ev.id, change_type="bus_to_car", valid_until=None, refund_percent=100, description="버스→자차: 차액 5,000원 환불(당일 포함)"),
    ])
    team = Team(event_id=ev.id, code="T01", name="합성팀")
    db.add(team)
    db.flush()
    names = ["한서윤", "박도윤", "이하준", "최지우"]
    people = []
    for i, n in enumerate(names):
        p = Participant(event_id=ev.id, team_id=team.id, employee_no=f"S{i}", name=n, email=f"syn{i}@example.com")
        db.add(p)
        db.flush()
        people.append(p)
        responses.submit_change(db, ev, p, "bus", source="web", actor="seed")
    db.add(TeamManager(team_id=team.id, participant_id=people[0].id, assigned_by=admin.id))
    responses.ensure_snapshot(db, ev, now=ev.attend_deadline)
    db.commit()
    clock.freeze(NOW)
    return ev, team, people


def summarize(v, n=160):
    s = json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + "…"


turns = []


def turn(label, ev, p, role, text, check=None):
    t0 = time.monotonic()
    out = communication.handle_message(db, ev, p, role, text)
    db.commit()
    from app.models import AgentRun

    run = db.query(AgentRun).order_by(AgentRun.id.desc()).first()
    rec = {
        "label": label,
        "role": role,
        "user": text,
        "decisions": run.result.get("decisions", []),
        "tool_steps": [{"tool": s["tool"], "dry_run": s.get("dry_run", False), "args": s.get("args"), "ok": s.get("ok"), "result_or_error": s.get("result") if s.get("ok") else s.get("error")} for s in run.steps],
        "reply": out["reply"]["text"],
        "state_after": out["thread"]["state"],
        "llm": run.result.get("llm"),
        "seconds": round(time.monotonic() - t0, 1),
    }
    if check:
        rec["db_check"] = check()
    turns.append(rec)
    print(f"\n### {label}\n사용자({role}): {text}")
    for d in rec["decisions"]:
        print(f"  [판단:{d['by']}] {summarize(d['action'])}")
    for s in rec["tool_steps"]:
        print(f"  [코드 실행] {s['tool']}{'(검증)' if s['dry_run'] else ''} ok={s['ok']} → {summarize(s['result_or_error'], 120)}")
    print(f"  [답변] {rec['reply'][:300]}")
    print(f"  상태={rec['state_after']} llm={rec['llm']} {rec['seconds']}s" + (f" DB={rec['db_check']}" if check else ""))
    time.sleep(1.5)
    return rec


def choice_of(pid):
    r = db.query(Response).filter_by(participant_id=pid).one()
    return r.choice


def changes_of(pid):
    return [(c.from_choice, c.to_choice, c.status, c.refund_amount, c.extra_charge) for c in db.query(ChangeLog).filter_by(participant_id=pid, after_deadline=True)]


ev, team, (mgr, p1, p2, p3) = build_world()

# 참가자 시나리오 ①~⑦
turn("①② 자연어 질문 → 조회 도구 선택", ev, p1, "participant", "제 참석 응답이랑 회비가 지금 어떻게 되어 있어요?")
turn("③ 모호한 변경 요청 → 추가 질문", ev, p1, "participant", "저 이번에 버스는 못 탈 것 같아요",
     check=lambda: {"choice": choice_of(p1.id), "changes": changes_of(p1.id)})
turn("④ 답변으로 대화 재개 → ⑤ 코드 계산 금액 확인", ev, p1, "participant", "제 차로 따로 갈게요",
     check=lambda: {"choice": choice_of(p1.id), "changes": changes_of(p1.id)})
turn("⑥⑦ 사용자 확인 → 변경 요청 기록·총무 승인 이관", ev, p1, "participant", "네",
     check=lambda: {"choice": choice_of(p1.id), "changes": changes_of(p1.id), "admin_tasks": [t.kind for t in db.query(ConfirmTask).filter_by(participant_id=p1.id)]})

# 잘못된 요청·모호한 확인
turn("권한: 다른 사람 변경 요청", ev, p2, "participant", f"{p3.name}님 응답을 불참으로 바꿔 주세요",
     check=lambda: {"p3_choice": choice_of(p3.id), "p3_changes": changes_of(p3.id)})
turn("모호한 확인 준비: 불참 요청", ev, p2, "participant", "회사 일정 때문에 워크숍 못 가요",
     check=lambda: {"choice": choice_of(p2.id), "changes": changes_of(p2.id)})
turn("모호한 확인 응답 → 실행 안 됨", ev, p2, "participant", "음… 잘 모르겠어요 아마도요?",
     check=lambda: {"choice": choice_of(p2.id), "changes": changes_of(p2.id)})

# 담당자: 여러 과제가 열린 상태의 '네'
run = Run(db, ev, "live-check")
yn = [{"key": "yes", "label": "네, 포함됨"}, {"key": "no", "label": "아니요"}]
a = ask_manager(run, team=team, kind="possible_duplicate", dedupe_key="live-a", question=f"{p2.name}님 개인 입금 20,000원이 팀 일괄 입금에 포함되어 있었나요?", options=yn, rule="R5", evidence={})
b = ask_manager(run, team=team, kind="possible_duplicate", dedupe_key="live-b", question=f"{p3.name}님 개인 입금 20,000원이 팀 일괄 입금에 포함되어 있었나요?", options=yn, rule="R5", evidence={})
db.commit()
open_tasks = lambda: {t.id: t.status for t in db.query(ConfirmTask).filter(ConfirmTask.id.in_([a.id, b.id]))}  # noqa: E731
turn("담당자: 과제 2개 열린 상태의 '네 맞아요'", ev, mgr, "manager", "네 맞아요", check=open_tasks)
turn("담당자: 번호로 답변 → 확인 질문", ev, mgr, "manager", f"#{a.id} 번은 포함된 거 맞아요", check=open_tasks)
turn("담당자: '네' → 그 과제만 답변", ev, mgr, "manager", "네", check=open_tasks)

clock.freeze(None)
out_dir = BACKEND_DIR / "var" / "llm_live"
out_dir.mkdir(parents=True, exist_ok=True)
stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
(out_dir / f"report-{stamp}.json").write_text(json.dumps(turns, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
used = sorted({d["by"] for t in turns for d in t["decisions"]})
print(f"\n판단 주체 종류: {used}")
print(f"보고서: {out_dir}/report-{stamp}.json")
