"""슬랙 실시간 연동(POST /api/large/slack/events)과 '비서 보고' 카드·브리핑·행동 뒤 결과 보고. 실제 Slack 연결 없이 서명된 가짜 이벤트로만."""
import hashlib
import hmac
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app import clock
from app.agents import admin_cards
from app.config import settings
from app.models import ChangeLog, EvidenceMessage

from .conftest import NOW
from .test_slack_evidence import EVENT_AT, changes_csv, demo, slack_csv, transactions_csv, up  # noqa: F401  (fixture 재사용)

SECRET = "test-signing-secret"


def signed(client, payload: dict, *, secret: str = SECRET, ts: int | None = None, sig: str | None = None):
    body = json.dumps(payload, ensure_ascii=False).encode()
    ts = ts if ts is not None else int(clock.now().replace(tzinfo=__import__("datetime").timezone.utc).timestamp())
    mac = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    # Slack 은 X-Requested-With 를 보내지 않는다 → 헤더 없이(서명만으로) 받아야 한다
    headers = {"X-Slack-Request-Timestamp": str(ts), "X-Slack-Signature": sig or mac, "Content-Type": "application/json", "X-Requested-With": ""}
    return client.post("/api/large/slack/events", content=body, headers=headers)


def msg_event(user: str, text: str, at, event_id: str, channel_type: str = "im") -> dict:
    ts = (at.replace(tzinfo=__import__("datetime").timezone.utc)).timestamp()
    return {"type": "event_callback", "event_id": event_id, "event": {"type": "message", "user": user, "text": text, "ts": f"{ts:.6f}", "channel": "D0001", "channel_type": channel_type}}


@pytest.fixture
def events_on(monkeypatch):
    monkeypatch.setattr(settings, "slack_events", True)
    monkeypatch.setattr(settings, "slack_signing_secret", SECRET)


def test_events_off_by_default_and_signature_required(db, client, demo, monkeypatch):
    assert signed(client, {"type": "url_verification", "challenge": "x"}).status_code == 404  # 기본 꺼짐
    monkeypatch.setattr(settings, "slack_events", True)
    assert signed(client, {"type": "url_verification", "challenge": "x"}).status_code == 403  # 비밀값 없음
    monkeypatch.setattr(settings, "slack_signing_secret", SECRET)
    assert signed(client, {"type": "url_verification", "challenge": "x"}, secret="wrong").status_code == 403
    old = int((clock.now() - timedelta(minutes=10)).replace(tzinfo=__import__("datetime").timezone.utc).timestamp())
    assert signed(client, {"type": "url_verification", "challenge": "x"}, ts=old).status_code == 403  # 5분 넘은 요청
    r = signed(client, {"type": "url_verification", "challenge": "abc123"})
    assert r.status_code == 200 and r.json() == {"challenge": "abc123"}


def test_live_event_equals_csv_replay(db, client, demo, events_on):
    """CSV 재생과 실시간 이벤트가 같은 함수(ingest_slack_message)를 거쳐 같은 제안을 만든다."""
    ev, base = demo
    # 실시간: 담당자 최라온(UTEST00004)이 DM 으로 '정마루님 … 버스 탔어요'
    r = signed(client, msg_event("UTEST00004", "정마루님 불참이라고 했었는데 오늘 일정이 비어서 버스 탔어요! 회비는 제가 걷어서 보낼게요", EVENT_AT - timedelta(minutes=25), "Ev1"))
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "change" and r.json()["outcome"] == "proposed"
    assert signed(client, msg_event("UTEST00004", "정마루님 불참이라고 했었는데 오늘 일정이 비어서 버스 탔어요!", EVENT_AT, "Ev1")).json() == {"ok": True, "duplicate": True}
    live = client.get(f"{base}/changes?status=pending_approval").json()
    assert [(c["participant_name"], c["extra_charge"], c["evidence"][0]["sender"]) for c in live] == [("정마루", 20000, "최라온")]
    db.expire_all()
    m = db.scalar(select(EvidenceMessage).where(EvidenceMessage.source == "slack_event"))
    assert m.participant_id is not None and m.team_id is not None
    # 명단에 없는 사람·봇·수정 이벤트는 무시(200)
    assert signed(client, msg_event("UNKNOWN", "버스 탈게요", EVENT_AT, "Ev2")).json()["ignored"] == "sender not in roster"
    bot = msg_event("UTEST00004", "x", EVENT_AT, "Ev3")
    bot["event"]["subtype"] = "message_changed"
    assert "ignored" in signed(client, bot).json()

    # 같은 내용을 CSV 로 재생 → 같은 사람·같은 금액의 제안(실시간 제안은 남아 있어 CSV 쪽은 기존 제안과 연결)
    out = up(client, f"{base}/demo/slack", slack_csv()).json()
    assert out["outcomes"] == {"linked": 1, "proposed": 2}, out
    db.rollback()  # 이 테스트 세션의 이전 읽기 스냅샷을 버리고 서버가 쓴 결과를 본다
    assert db.scalar(select(func.count()).select_from(ChangeLog).where(ChangeLog.source == "slack", ChangeLog.status == "pending_approval")) == 3


def test_csv_replay_is_same_as_batch_reread(db, client, demo):
    ev, base = demo
    first = up(client, f"{base}/demo/slack", slack_csv()).json()
    assert first["outcomes"] == {"proposed": 3}
    up(client, f"{base}/demo/changes", changes_csv())  # 변경 CSV → 메시지를 다시 읽어 기록과 연결
    db.expire_all()
    linked = db.scalar(select(func.count()).select_from(EvidenceMessage).where(EvidenceMessage.change_id.is_not(None)))
    again = up(client, f"{base}/demo/slack", slack_csv()).json()  # 재생 다시
    assert again["outcomes"] == {"linked": 3}
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(EvidenceMessage).where(EvidenceMessage.change_id.is_not(None))) == linked


def test_briefing_cards_and_action_report(db, client, demo):
    ev, base = demo
    up(client, f"{base}/demo/slack", slack_csv())
    b = client.get(f"{base}/agent/briefing").json()
    assert b["type"] == "briefing" and b["decisions"] == 3
    # 가장 급한 것: 규칙으로 금액이 정해지는 제안(바로 승인 가능)부터
    assert b["urgent"]["action"] == "approve_change" and "승인(" in b["urgent"]["label"]
    assert any("입금 대조 전" in x["text"] for x in b["items"])
    assert client.post(f"{base}/agent/briefing/seen").status_code == 200
    assert client.get(f"{base}/agent/briefing").json()["greeting"].endswith("지난번 이후 이런 일이 있었어요.")

    # 질문 → 카드: 승인 전이면 결론에 '(총무님 승인 전)', 거래내역 전이면 입금·잔액 숨김
    c = client.post(f"{base}/agent/chat", json={"text": "개발팀 왜 추가 납부야?"}).json()["reply"]["card"]
    assert "총무님 승인 전" in c["headline"] and c["status"]["key"] == "check" and not c["money"]["show_received"] and "balance" not in c["money"]
    assert c["money"]["if_approved"] == c["money"]["final"] + 15000  # 승인되면(+20,000 −5,000)
    assert [t["action"] for t in c["todo"]][:2] == ["approve_change", "approve_change"]

    # 총무가 승인 → Agent 가 채팅에 결과 보고
    pend = {x["participant_name"]: x for x in client.get(f"{base}/changes?status=pending_approval").json()}
    r = client.post(f"{base}/changes/{pend['정마루']['id']}/approve", json={"note": ""}).json()
    head = r["agent_report"]["card"]["headline"]
    assert head.startswith("정마루님 불참→참석+버스 승인했어요(추가 20,000원).") and "개발팀 최종 회비가" in head and "보고서 v" in head
    msgs = client.get(f"{base}/agent/chat").json()["messages"]
    assert msgs[-1]["card"]["type"] == "result"

    # 여러 부서: 한 줄 요약 + 가장 먼저 볼 것
    c = client.post(f"{base}/agent/chat", json={"text": "확인 필요한 부서와 이유"}).json()["reply"]["card"]
    assert c["type"] == "teams" and c["first"] and c["headline"].startswith(f"확인 필요 {len(c['rows'])}곳 중 먼저 볼 곳은")


def test_after_transactions_card_shows_balance_and_notify(db, client, demo):
    ev, base = demo
    up(client, f"{base}/demo/changes", changes_csv())
    up(client, f"{base}/demo/slack", slack_csv())
    up(client, f"{base}/transactions", transactions_csv())
    client.post(f"{base}/agent/settlement-review")
    client.post(f"{base}/allocations/confirm-high")
    c = client.post(f"{base}/agent/chat", json={"text": "개발팀 왜 추가 납부야?"}).json()["reply"]["card"]
    assert c["status"]["key"] == "extra" and c["money"]["show_received"] and c["money"]["balance"] == 15000
    assert "담당자 최라온님께 안내하면 돼요" in c["headline"] and c["todo"][-1]["action"] == "notify_preview"
    c = client.post(f"{base}/agent/chat", json={"text": "확인 필요한 부서와 이유"}).json()["reply"]["card"]
    assert c["first"]["team"] in ("물류팀", "기획팀") and "슬랙과 일치" in c["first"]["reason"]


def test_llm_card_numbers_are_checked():
    template = {"greeting": "총무님,", "headline": "20,000원 추가", "story": [], "money": {"billed": 40000, "final": 60000}}
    steps = [{"tool": "get_evidence", "ok": True, "result": {"final_fee": 60000, "changes": [{"delta": 20000}]}}]
    ok, why = admin_cards.check_llm_card({"greeting": "총무님!", "headline": "홍주원님 당일 탑승으로 20,000원 추가예요"}, template, steps)
    assert not why and ok["headline"].startswith("홍주원님") and ok["by_llm"]
    bad, why = admin_cards.check_llm_card({"headline": "25,000원 추가 납부가 필요해요"}, template, steps)
    assert bad is template and "25,000" in why
