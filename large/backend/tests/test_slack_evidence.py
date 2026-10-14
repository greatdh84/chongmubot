"""슬랙 대화 CSV(근거 메시지) → 정산 Agent 변경 제안·입금자 근거 → 정산 보고서 '왜?'. 작은 가짜 CSV 로 시연 흐름을 재현한다."""
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.adapters import llm as llm_mod
from app.config import settings
from app.models import Admin, ChangeLog, Event, EvidenceMessage, RefundRule
from app.security import hash_password

from .conftest import NOW

# NOW = 10/8 11:00 KST. 행사는 이미 끝났다(정산 단계).
ATTEND = NOW - timedelta(days=10)
PAYMENT = NOW - timedelta(days=7)
EVENT_AT = NOW - timedelta(days=1)


def kst(dt):
    return (dt + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M")


ROSTER = """ID,이름,전화번호,Slack ID,소속 부서,팀 담당자
E001,김가람,010-0000-0001,UTEST00001,경영팀,김가람
E002,이나래,010-0000-0002,UTEST00002,경영팀,김가람
E003,박다솜,010-0000-0003,UTEST00003,경영팀,김가람
E004,최라온,010-0000-0004,UTEST00004,개발팀,최라온
E005,정마루,010-0000-0005,UTEST00005,개발팀,최라온
E006,한바다,010-0000-0006,UTEST00006,개발팀,최라온
E007,오새봄,010-0000-0007,UTEST00007,물류팀,오새봄
E008,신기훈,010-0000-0008,UTEST00008,기획팀,신기훈
"""


def responses_csv():
    t = kst(ATTEND - timedelta(hours=5))
    return f"""ID,이름,소속 부서,응답,응답일시
E001,김가람,경영팀,참석+버스,{t}
E002,이나래,경영팀,참석+버스,{t}
E003,박다솜,경영팀,,
E004,최라온,개발팀,참석+버스,{t}
E005,정마루,개발팀,불참,{t}
E006,한바다,개발팀,참석+버스,{t}
E007,오새봄,물류팀,참석+버스,{t}
E008,신기훈,기획팀,참석+자차,{t}
"""


def slack_csv():
    d = EVENT_AT
    return f"""일시,보낸 사람,받는 사람,부서,관련 ID,내용
{kst(d - timedelta(minutes=25))},최라온,총무,개발팀,E005,정마루님 불참이라고 했었는데 오늘 일정이 비어서 버스 탔어요! 회비는 제가 걷어서 보낼게요
{kst(d - timedelta(minutes=20))},최라온,총무,개발팀,E006,한바다님 버스 안 타고 본인 차로 왔어요. 버스 명단에서 빼 주세요
{kst(d + timedelta(minutes=5))},김가람,총무,경영팀,E002,이나래님이 아침에 급한 집안일로 못 온대요. 회비 돌려받을 수 있을까요?
{kst(d + timedelta(minutes=30))},총무,김가람,경영팀,E002,버스는 이미 출발해서 식대 10,000원만 환불할게요
{kst(PAYMENT - timedelta(hours=20))},신기훈,총무,기획팀,,기획팀 회비 토스로 보냈습니다
{kst(d + timedelta(hours=9))},오새봄,총무,물류팀,,저희 팀 회비는 제 아내 계좌로 'ㅎㅂ' 이름으로 보냈어요
{kst(PAYMENT - timedelta(hours=9))},총무봇,김가람,경영팀,,오늘 18시가 회비 납부 마감이에요
"""


def transactions_csv():
    p = PAYMENT
    return f"""거래일시,입금자명,금액,메모
{kst(p - timedelta(hours=5))},김가람,40000,T01 워크숍회비
{kst(p - timedelta(hours=5))},최라온,40000,T02 워크숍회비
{kst(p - timedelta(hours=6))},ㅎㅂ,20000,
{kst(p - timedelta(hours=7))},토스 신*훈,15000,
{kst(p + timedelta(days=2))},김가람,20000,T01 추가 1명
"""


def changes_csv():
    d = EVENT_AT
    return f"""ID,이름,소속 부서,변경 전,변경 후,요청일시,경로,처리,처리일시,환불액(규칙 없을 때),메모
E003,박다솜,경영팀,미응답,참석+버스,{kst(PAYMENT + timedelta(days=1))},총무 직접 입력,승인,{kst(PAYMENT + timedelta(days=1, minutes=5))},,전화로 버스
E005,정마루,개발팀,불참,참석+버스,{kst(d - timedelta(minutes=25))},행사 당일(총무 입력),승인,{kst(d - timedelta(minutes=20))},,당일 버스 탑승
E006,한바다,개발팀,참석+버스,참석+자차,{kst(d - timedelta(minutes=20))},행사 당일(총무 입력),승인,{kst(d - timedelta(minutes=10))},,당일 자차
E002,이나래,경영팀,참석+버스,불참,{kst(d + timedelta(minutes=5))},행사 당일(총무 입력),승인,{kst(d + timedelta(minutes=30))},10000,당일 노쇼 — 식대만 환불
"""


@pytest.fixture
def demo(db, client):
    admin = db.scalar(select(Admin).where(Admin.email == "admin@example.com"))
    if admin is None:
        admin = Admin(email="admin@example.com", password_hash="")
        db.add(admin)
    admin.password_hash = hash_password("pw-123456")
    db.flush()
    ev = Event(admin_id=admin.id, name="시연 워크숍", event_at=EVENT_AT, attend_deadline=ATTEND, payment_deadline=PAYMENT, fee_bus=20000, fee_car=15000, fee_absent=0)
    db.add(ev)
    db.flush()
    db.add_all([
        RefundRule(event_id=ev.id, change_type="attend_to_absent", valid_until=PAYMENT, refund_percent=100, description="납부 마감 전 불참 100%"),
        RefundRule(event_id=ev.id, change_type="attend_to_absent", valid_until=EVENT_AT - timedelta(days=3), refund_percent=50, description="행사 3일 전까지 50%"),
        RefundRule(event_id=ev.id, change_type="bus_to_car", valid_until=None, refund_percent=100, description="버스→자차: 차액 5,000원 환불(당일 포함)"),
    ])
    db.commit()
    assert client.post("/api/large/auth/admin/login", json={"email": "admin@example.com", "password": "pw-123456"}).status_code == 200
    base = f"/api/large/admin/events/{ev.id}"
    up(client, f"{base}/roster", ROSTER)
    assert up(client, f"{base}/demo/responses", responses_csv()).status_code == 200
    return ev, base


def up(client, path, text):
    return client.post(path, files={"file": ("x.csv", text.encode("utf-8"), "text/csv")})


def teams(client, base):
    rep = client.get(f"{base}/report").json()
    return rep, {t["name"]: t for t in rep["teams"]}


def test_slack_only_makes_hitl_proposals_with_rule_amounts(db, client, demo):
    ev, base = demo
    r = up(client, f"{base}/demo/slack", slack_csv())
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["counts"]["change"] == 3 and out["outcomes"] == {"proposed": 3}
    assert out["counts"]["deposit_hint"] == 2 and out["counts"]["admin_refund"] == 1 and out["counts"]["bot_dm"] == 1
    pend = {c["participant_name"]: c for c in client.get(f"{base}/changes?status=pending_approval").json()}
    # 자동 확정 없음: 셋 다 승인 대기, 응답은 그대로
    assert set(pend) == {"정마루", "한바다", "이나래"}
    assert pend["정마루"]["extra_charge"] == 20000 and pend["정마루"]["from_choice"] == "absent"  # '불참이라고 했었는데 … 버스 탔어요' → 버스
    assert pend["한바다"]["refund_amount"] == 5000 and not pend["한바다"]["rule_missing"]  # 당일 버스→자차 규칙
    assert pend["이나래"]["rule_missing"] and pend["이나래"]["suggested_refund"] == 10000  # 당일 노쇼: 규칙 없음 + 슬랙 총무 결정 제안
    assert pend["정마루"]["evidence"][0]["sender"] == "최라온"
    s = client.get(f"{base}/report").json()["summary"]
    assert s["team_status"]["check"] >= 2
    # 총무 승인(제안 금액 그대로) → 반영
    for name in ("정마루", "한바다"):
        assert client.post(f"{base}/changes/{pend[name]['id']}/approve", json={"note": ""}).status_code == 200
    c = pend["이나래"]
    assert client.post(f"{base}/changes/{c['id']}/approve", json={"note": c["suggested_note"], "refund_amount": c["suggested_refund"]}).status_code == 200
    db.expire_all()
    noshow = db.get(ChangeLog, c["id"])
    assert noshow.admin_refund_decided and noshow.refund_amount == 10000


def test_changes_csv_then_slack_links_and_is_idempotent(db, client, demo):
    ev, base = demo
    assert up(client, f"{base}/demo/changes", changes_csv()).status_code == 200
    first = up(client, f"{base}/demo/slack", slack_csv()).json()
    assert first["outcomes"] == {"linked": 3}  # 이미 기록된 변경과 연결, 새 제안 없음
    again = up(client, f"{base}/demo/slack", slack_csv()).json()
    assert again["counts"] == first["counts"] and again["outcomes"] == first["outcomes"]
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(EvidenceMessage).where(EvidenceMessage.event_id == ev.id)) == 7
    assert db.scalar(select(func.count()).select_from(ChangeLog).where(ChangeLog.event_id == ev.id, ChangeLog.source == "slack")) == 0
    # 변경 CSV 를 다시 넣어도 메시지는 새 기록에 다시 이어진다
    assert up(client, f"{base}/demo/changes", changes_csv()).status_code == 200
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(EvidenceMessage).where(EvidenceMessage.change_id.is_not(None))) == 4  # 변경 3 + 총무 결정 1

    rep, t = teams(client, base)
    dev = t["개발팀"]
    assert (dev["billed"], dev["final_fee"]) == (40000, 55000)  # 정마루 불참→버스 +20,000, 한바다 버스→자차 −5,000
    people = {p["name"]: p for p in rep["people"]}
    assert people["한바다"]["amount"] == 5000 and people["한바다"]["on_event_day"] and people["한바다"]["basis_kind"] == "rule"
    assert people["이나래"]["basis_kind"] == "admin" and "식대 10,000원" in people["이나래"]["slack"]["text"]
    assert people["정마루"]["slack"]["sender"] == "최라온"
    assert people["박다솜"]["kind"] == "extra"  # 청구 때 미응답 → 납부 마감 뒤 버스: 전액 추가


def test_deposits_slack_suggestions_late_payment_and_evidence(db, client, demo):
    ev, base = demo
    up(client, f"{base}/demo/changes", changes_csv())
    up(client, f"{base}/demo/slack", slack_csv())
    assert up(client, f"{base}/transactions", transactions_csv()).status_code == 200
    review = client.post(f"{base}/agent/settlement-review").json()
    assert review["counts"].get("slack_suggested") == 2
    client.post(f"{base}/allocations/confirm-high")
    tasks = [t for t in client.get(f"{base}/tasks?status=active").json() if t["kind"] == "unknown_depositor"]
    sug = {t["evidence"]["depositor"]: t["evidence"]["slack_suggestion"] for t in tasks}
    assert sug["ㅎㅂ"]["team"] == "물류팀" and sug["토스 신*훈"]["team"] == "기획팀"  # 입금자명 직접 언급 / 토스 + 가린 이름 = 보낸 사람
    _, t = teams(client, base)
    assert t["물류팀"]["status"] == "check" and t["기획팀"]["status"] == "check"  # 근거만 붙이고 배정은 안 함(HITL)

    card = client.get(f"{base}/agent/settlement-card").json()
    assert card["total"] == 5 and card["auto"] == 3 and card["reasons"] == {"입금자 확인": 2}

    for task in tasks:
        assert client.post(f"{base}/tasks/{task['id']}/accept-suggestion").status_code == 200
    assert client.post(f"{base}/tasks/{tasks[0]['id']}/accept-suggestion").status_code == 409  # 이미 처리
    _, t = teams(client, base)
    assert t["물류팀"]["status"] == "done" and t["기획팀"]["status"] == "done"

    # 마감 후 추가 입금 → '추가 납부 완료' 표시, 근거 패널의 청구 → 최종 검산
    ev_mg = client.get(f"{base}/report/teams/{t['경영팀']['team_id']}/evidence").json()
    late = [r for r in ev_mg["receipts"] if r["after_deadline"]]
    assert len(late) == 1 and late[0]["late_label"].startswith("추가 납부 완료")
    assert ev_mg["check"]["ok"] and ev_mg["billing"]["billed"] == 40000
    assert any(m["sender"] == "총무" for m in ev_mg["messages"])

    tl = client.get(f"{base}/agent/timeline").json()
    phases = {s["phase"] for x in tl for s in x["steps"]}
    assert {"감지", "판단", "실행", "넘김"} <= phases
    assert any(x["title"].startswith("참석 변경 감지") for x in tl)


def test_agent_explains_why_with_slack_and_time(db, client, demo):
    ev, base = demo
    up(client, f"{base}/demo/changes", changes_csv())
    up(client, f"{base}/demo/slack", slack_csv())
    r = client.post(f"{base}/agent/chat", json={"text": "개발팀 왜 추가 납부야?"}).json()["reply"]
    assert [x["tool"] for x in r["tools"]] == ["get_evidence"]
    c = r["card"]
    assert c["greeting"] == "총무님, 개발팀 정리해 드릴게요." and c["status"]["label"] == "추가 납부"
    assert "입금 대조 전" in c["headline"]  # 거래내역 전: 잔액 줄은 숨기고 그 사실을 결론에
    assert "received" not in c["money"] and (c["money"]["billed"], c["money"]["final"], c["money"]["diff"]) == (40000, 55000, 15000)
    hong = next(x for x in c["story"] if x["text"].startswith("정마루님"))
    assert hong["who"] == "담당자 최라온님 슬랙" and "버스 탔어요" in hong["quote"] and "+20,000원" in hong["text"]
    assert c["sources"]["slack"] >= 2 and c["todo"][0]["action"] == "open_payments"
    r = client.post(f"{base}/agent/chat", json={"text": "최라온 팀 현황"}).json()["reply"]
    assert [x["tool"] for x in r["tools"]] == ["get_department", "get_evidence"]


def test_notify_preview_then_send_shows_in_evidence(db, client, demo):
    ev, base = demo
    up(client, f"{base}/demo/changes", changes_csv())
    up(client, f"{base}/transactions", transactions_csv())
    client.post(f"{base}/agent/settlement-review")
    client.post(f"{base}/allocations/confirm-high")
    pv = client.post(f"{base}/emails/preview", json={"kind": "extra_notice"}).json()
    dev = next(x for x in pv["targets"] if x["team"] == "개발팀")
    assert dev["manager"] == "최라온" and dev["amount"] == 15000 and "15,000원을 추가로" in dev["body"] and not dev["already_sent"]
    assert {m["name"] for m in dev["members"]} == {"정마루", "한바다"}
    client.post(f"{base}/emails/enqueue", json={"kind": "extra_notice"})
    client.post(f"{base}/emails/dispatch")
    assert next(x for x in client.post(f"{base}/emails/preview", json={"kind": "extra_notice"}).json()["targets"] if x["team"] == "개발팀")["already_sent"]
    sent = client.get(f"{base}/emails/sent?kind=extra_notice").json()
    assert any(j["team"] == "개발팀" and j["body"] for j in sent)
    msgs = client.get(f"{base}/evidence/messages?team_id={dev['team_id']}").json()
    assert any(m["source"] == "dm_job" and m["kind"] == "extra_notice" for m in msgs)


def test_bad_slack_rows_rejected_with_line_numbers(db, client, demo):
    ev, base = demo
    bad = slack_csv().replace("최라온,총무,개발팀,E005", "최라온,총무,없는팀,E005").replace("총무,김가람,경영팀,E002", "총무,김가람,경영팀,E999")
    r = up(client, f"{base}/demo/slack", bad)
    assert r.status_code == 422
    lines = {e["line"]: e["error"] for e in r.json()["error"]["detail"]}
    assert set(lines) == {2, 5} and "부서" in lines[2] and "ID" in lines[5]
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(EvidenceMessage)) == 0


def test_llm_extraction_is_validated_before_use(db, client, demo, monkeypatch):
    ev, base = demo
    monkeypatch.setattr(settings, "llm_mode", "claude")
    seen = {}

    def fake(system, payload):  # 실시간과 같이 메시지 한 건씩 호출된다
        seen.setdefault("people", set()).update(p["employee_no"] for p in payload["people"])
        m = payload["messages"][0]
        items = {
            "정마루님": {"kind": "change", "employee_no": "E005", "from": "absent", "to": "bus", "confidence": "high", "reason": "버스 탔어요"},
            "한바다님": {"kind": "change", "employee_no": "E999", "to": "car", "confidence": "high"},  # 명단에 없는 사람 → 버림
            "이나래님": {"kind": "change", "employee_no": "E006", "to": "absent", "confidence": "high"},  # 관련 ID(E002)와 다른 사람 → 버림
        }
        hit = next((v for k, v in items.items() if k in m["text"]), None)
        return {"items": [{"message_id": m["message_id"], **hit}] if hit else []}, "claude-test"

    monkeypatch.setattr("app.services.slack.complete_json", fake)
    out = up(client, f"{base}/demo/slack", slack_csv()).json()
    assert out["by"] == "claude:claude-test" and out["outcomes"] == {"proposed": 1}
    assert seen["people"] >= {"E005", "E002"}
    pend = client.get(f"{base}/changes?status=pending_approval").json()
    assert [c["participant_name"] for c in pend] == ["정마루"]

    def boom(system, payload):
        raise llm_mod.LLMError("Claude HTTP 529")

    monkeypatch.setattr("app.services.slack.complete_json", boom)
    out = up(client, f"{base}/demo/slack", slack_csv()).json()
    assert out["by"] == "mock_fallback" and "529" in out["fallback"] and out["outcomes"] == {"proposed": 3}  # 규칙 기반으로 대체


def test_slack_upload_not_available_in_production(db, client, demo, monkeypatch):
    ev, base = demo
    monkeypatch.setattr(settings, "app_mode", "production")
    assert up(client, f"{base}/demo/slack", slack_csv()).status_code == 404
