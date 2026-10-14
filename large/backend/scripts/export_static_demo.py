"""GitHub Pages 정적 데모판용 응답 녹화. 임시 DB 에 시드(행사 다음 날 상태)를 만들고, 총무 화면이 부르는 GET 응답과
총무 Agent 답을 TestClient 로 받아 demo/large/fixtures.json · report.csv 로 저장한다. 시연 DB(var/)는 건드리지 않는다.

    ../../.venv/bin/python scripts/export_static_demo.py [--base-date 2026-10-15] [--out ../../demo/large]

실제 Slack·LLM 키는 쓰지 않는다(LARGE_SKIP_ROOT_ENV=1, NOTIFY_MODE=mock, LLM_MODE=mock).
시각은 기준일 10:00(KST)로 고정해, 코드가 같으면 같은 파일이 나온다(자동 갱신 때 불필요한 커밋 방지).
금액은 서버 코드가 계산한 응답을 그대로 녹화한다(바꾸지 않음).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

# 설정을 읽기 전에 환경을 고정한다(실제 키·발송·스케줄러 금지)
for k in ("SLACK_BOT_TOKEN", "LLM_API_KEY", "GEMINI_API_KEY", "DATABASE_URL"):
    os.environ.pop(k, None)
os.environ.update({"LARGE_SKIP_ROOT_ENV": "1", "APP_MODE": "demo", "NOTIFY_MODE": "mock", "LLM_MODE": "mock", "NOTIFY_SCHEDULER": "off"})

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent.parent
sys.path.insert(0, str(BACKEND))

ap = argparse.ArgumentParser(description="대모임 정적 데모판 응답 녹화")
ap.add_argument("--base-date", default="2026-10-15", help="'오늘'(KST). 행사는 전날 끝난 상태로 만든다")
ap.add_argument("--out", default=str(REPO / "demo" / "large"))
args = ap.parse_args()

from fastapi.testclient import TestClient  # noqa: E402

from app import clock, db as dbmod, seed as seedmod  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import create_app  # noqa: E402

settings.app_mode, settings.email_mode, settings.llm_mode, settings.email_scheduler = "demo", "mock", "mock", False
settings.slack_bot_token = settings.claude_api_key = settings.gemini_api_key = ""

# '지금'을 기준일 10:00 KST 로 고정. 시드가 끝에 freeze(None)(실제 현재)로 돌리는 것도 이 시각으로 바꾼다.
base = date.fromisoformat(args.base_date)
NOW = clock.kst_naive_to_utc(datetime(base.year, base.month, base.day, 10, 0))
_freeze = clock.freeze
clock.freeze = lambda dt: _freeze(NOW if dt is None else dt)  # type: ignore[assignment]
clock.freeze(None)

tmp = Path(tempfile.mkdtemp(prefix="static-demo-"))
seedmod.SAMPLES_DIR = tmp / "samples"
dbmod.configure(f"sqlite:///{tmp / 'demo.db'}")
seedmod.seed(base, stage="after-event")

# 총무 Agent 에 미리 물어 둘 질문(화면 예시 버튼 6개 + 부서별·사람별). 확인 흐름은 맨 끝에 '아니요'·'네' 둘 다 녹화.
QUESTIONS = [
    "경영지원팀의 추가 회비 납부자를 알려줘", "환불할 부서 목록", "확인 필요한 부서", "당일 변동 있던 사람", "지난 보고서 이후 바뀐 것", "전체 요약",
    "인사팀 현황", "재무팀 현황", "영업1팀 현황", "해외영업팀 현황", "추가 납부할 부서 목록", "승인 대기 사람 목록", "응답 미확정 사람 목록",
    "데이터팀 왜 추가 납부야?", "환불 대상 부서 알려줘", "확인 필요한 부서와 이유", "박채원 팀 현황", "재무팀 왜 정산 완료야?",
]
CONFIRM = ["환불 대상 담당자에게 안내 보내줘", "추가 납부 대상 담당자에게 안내 보내줘"]


def key(path: str, params: dict[str, str] | None = None) -> str:
    """frontend/src/staticDemo.ts 의 fixtureKey 와 같은 규칙: 빈 값 제거·이름순 정렬."""
    items = sorted((k, v) for k, v in (params or {}).items() if v not in ("", None))
    return path + ("?" + "&".join(f"{k}={quote(str(v), safe='')}" for k, v in items) if items else "")


def norm(text: str) -> str:
    """staticDemo.ts 의 normQuestion 과 같은 규칙: 공백 정리, 끝 문장부호 제거."""
    return " ".join(text.split()).rstrip("?.!~ ")


with TestClient(create_app()) as c:
    c.headers.update({"X-Requested-With": "chongmu"})
    r = c.post("/api/large/auth/admin/login", json={"email": settings.admin_email, "password": settings.admin_password})
    assert r.status_code == 200, r.text

    def get(path: str, params: dict[str, str] | None = None):
        q = {k: v for k, v in (params or {}).items() if v not in ("", None)}
        res = c.get(path, params=q)
        assert res.status_code == 200, (path, q, res.status_code, res.text[:300])
        return res.json()

    events = get("/api/large/admin/events")
    ev = events[0]["id"]
    b = f"/api/large/admin/events/{ev}"
    teams = get(f"{b}/teams")
    plan: list[tuple[str, dict[str, str]]] = [
        ("/api/large/meta", {}), ("/api/large/admin/events", {}), (b, {}), (f"{b}/report", {}), (f"{b}/refund-rules", {}),
        (f"{b}/teams", {}), (f"{b}/participants", {}), (f"{b}/transactions", {}), (f"{b}/emails/scheduler", {}), (f"{b}/agent/chat", {}),
        *[(f"{b}/changes", {"status": s}) for s in ("pending_approval", "", "applied", "rejected")],
        *[(f"{b}/tasks", {"status": s, "assignee": a}) for s in ("active", "", "resolved") for a in ("", "admin", "manager")],
        *[(f"{b}/agent-runs", {"agent": a}) for a in ("", "admin", "settlement", "communication")],
        *[(f"{b}/emails", {"status": s}) for s in ("", "queued", "retry_wait", "sending", "sent", "skipped", "failed")],
        *[(f"{b}/settlements/{t['id']}", {}) for t in teams],
        *[(f"{b}/report/teams/{t['id']}/evidence", {}) for t in teams],
        (f"{b}/agent/timeline", {}), (f"{b}/agent/settlement-card", {}), (f"{b}/emails/sent", {}), (f"{b}/agent/briefing", {}),
        (f"{b}/workbench", {}), (f"{b}/refunds", {}),
    ]
    for p, q in plan:  # 한 번 돌려 기준표·보고서를 먼저 만들어 두고(첫 열람 때 생기는 버전), 두 번째 결과를 녹화
        get(p, q)
    fixtures: dict[str, object] = {key(p, q): get(p, q) for p, q in plan}
    report_csv = c.get(f"{b}/report.csv").content

    # 총무 Agent: 질문마다 새로 붙은 메시지와 끝 상태를 녹화(화면에서는 이 메시지를 대화에 이어 붙인다)
    def ask(text: str) -> dict:
        before = len(get(f"{b}/agent/chat")["messages"])
        res = c.post(f"{b}/agent/chat", json={"text": text})
        assert res.status_code == 200, res.text
        th = res.json()["thread"]
        return {"messages": th["messages"][before:], "state": th["state"], "pending": th["pending"]}

    answers: dict[str, dict] = {}
    for q in QUESTIONS:
        a = ask(q)
        assert a["state"] == "idle", (q, a["state"])
        answers[norm(q)] = a
    confirms: dict[str, dict] = {}
    for q in CONFIRM:  # 아니요 먼저(아무것도 안 바뀜), 그다음 같은 질문 → 네(모의 발송)
        first = ask(q)
        assert first["state"] == "awaiting_confirmation", (q, first)
        no = ask("아니요")
        again = ask(q)
        assert [m["text"] for m in again["messages"]] == [m["text"] for m in first["messages"]], q
        yes = ask("네")
        confirms[norm(q)] = {"ask": first, "yes": yes, "no": no}

out = Path(args.out)
out.mkdir(parents=True, exist_ok=True)
data = {
    "about": "대모임 총무 화면 정적 데모판 녹화(가상 데이터). large/backend/scripts/export_static_demo.py 가 만듦 — 직접 고치지 말 것",
    "base_date": base.isoformat(), "now": clock.fmt(NOW), "event_base": b,
    "get": fixtures, "chat": {"questions": QUESTIONS + CONFIRM, "answers": answers, "confirms": confirms},
}
(out / "fixtures.json").write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")
(out / "report.csv").write_bytes(report_csv)
print(f"녹화 완료: GET {len(fixtures)}개 · Agent 질문 {len(answers)}개 + 확인 흐름 {len(confirms)}개 → {out}")
