"""시연용 CSV 4단계(명단 → 응답 → 변경 → 거래내역) 통합 흐름. 실제 CSV 대신 같은 구조의 작은 가짜 CSV 를 쓴다."""
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.models import Admin, ChangeLog, Event, RefundRule, Response, TeamBilling
from app.security import hash_password

from .conftest import NOW

ATTEND = NOW - timedelta(days=6)
PAYMENT = NOW - timedelta(days=3)
EVENT_AT = NOW + timedelta(days=2)  # 행사 3일 전(=NOW-1일)까지 50% 규칙이 납부 마감 뒤에도 적용되게


def kst(dt):
    """naive UTC → CSV 에 적는 KST 'YYYY-MM-DD HH:MM'"""
    return (dt + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M")


ROSTER = """ID,이름,전화번호,Slack ID,소속 부서,팀 담당자
E001,김가람,010-0000-0001,UTEST00001,경영팀,김가람
E002,이나래,010-0000-0002,UTEST00002,경영팀,김가람
E003,박다솜,010-0000-0003,UTEST00003,경영팀,김가람
E004,최라온,010-0000-0004,UTEST00004,개발팀,최라온
E005,정마루,010-0000-0005,UTEST00005,개발팀,최라온
E006,한바다,010-0000-0006,UTEST00006,개발팀,최라온
"""


def responses_csv():
    t = lambda h: kst(ATTEND - timedelta(hours=h))  # noqa: E731
    return f"""ID,이름,소속 부서,응답,응답일시
E001,김가람,경영팀,참석+버스,{t(30)}
E002,이나래,경영팀,참석+자차,{t(20)}
E003,박다솜,경영팀,미정,{t(10)}
E004,최라온,개발팀,참석+버스,{t(40)}
E005,정마루,개발팀,참석+버스,{t(5)}
E006,한바다,개발팀,,
"""


def changes_csv():
    a, p = ATTEND, PAYMENT
    return f"""ID,이름,소속 부서,변경 전,변경 후,요청일시,경로,처리,처리일시,환불액(규칙 없을 때),메모
E003,박다솜,경영팀,미정,참석+버스,{kst(a + timedelta(hours=1))},총무 직접 입력,승인,{kst(a + timedelta(hours=1, minutes=10))},,전화로 버스 확정
E006,한바다,개발팀,미응답,참석+버스,{kst(a + timedelta(hours=2))},총무 직접 입력,승인,{kst(a + timedelta(hours=2, minutes=5))},,응답 없어 전화로 확인
E005,정마루,개발팀,참석+버스,불참,{kst(p + timedelta(hours=1))},참가자 Agent 채팅,승인,{kst(p + timedelta(hours=3))},,가족 행사
E002,이나래,경영팀,참석+자차,불참,{kst(p + timedelta(hours=2))},참가자 Agent 채팅,승인 대기,,,
"""


def transactions_csv():
    return f"""거래일시,입금자명,금액,메모
{kst(PAYMENT - timedelta(hours=5))},김가람,55000,T01 워크숍회비
{kst(PAYMENT - timedelta(hours=4))},최라온,60000,T02 워크숍회비
"""


@pytest.fixture
def demo(db, client):
    admin = db.scalar(select(Admin).where(Admin.email == "admin@example.com"))  # 앱 시작 때 만든 시연 총무(없으면 새로)
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
        RefundRule(event_id=ev.id, change_type="bus_to_car", valid_until=PAYMENT, refund_percent=100, description="납부 마감 전 버스→자차 100%"),
        RefundRule(event_id=ev.id, change_type="bus_to_car", valid_until=None, refund_percent=0, description="이후 0%"),
    ])
    db.commit()
    assert client.post("/api/large/auth/admin/login", json={"email": "admin@example.com", "password": "pw-123456"}).status_code == 200
    return ev, f"/api/large/admin/events/{ev.id}"


def up(client, path, text, name="x.csv"):
    return client.post(path, files={"file": (name, text.encode("utf-8"), "text/csv")})


def test_four_csv_flow_reproduces_report(db, client, demo):
    ev, base = demo
    r = up(client, f"{base}/roster", ROSTER).json()
    assert (r["rows"], r["teams"], r["managers_total"]) == (6, 2, 2)
    assert up(client, f"{base}/roster", ROSTER).json()["managers_total"] == 2  # 다시 올려도 '지금 담당자 있는 부서 수'

    r = up(client, f"{base}/demo/responses", responses_csv())
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["basis"] == "참석 마감 기준"
    assert out["counts"] == {"bus": 3, "car": 1, "absent": 0, "undecided": 1, "none": 1}

    r = up(client, f"{base}/demo/changes", changes_csv())
    assert r.status_code == 200, r.text
    first = r.json()
    assert first["changes"] == {"applied": 3, "pending": 1, "rejected": 0}
    assert first["counts"] == out["counts"]  # 마감 기준 응답은 변경으로 바뀌지 않음
    assert first["attending_now"] == 5  # 박다솜·한바다 버스 확정, 정마루 불참, 이나래(승인 대기)는 아직 자차
    # 같은 파일을 다시 넣어도 같은 결과(기록이 쌓이지 않음)
    assert up(client, f"{base}/demo/changes", changes_csv()).json() == first
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(ChangeLog).where(ChangeLog.event_id == ev.id, ChangeLog.after_deadline.is_(True))) == 4
    assert db.scalar(select(func.count()).select_from(TeamBilling).where(TeamBilling.event_id == ev.id)) == 2
    # 납부 마감 청구표: 마감 전 승인된 변경(박다솜·한바다 버스)까지 반영, 마감 후 변경은 반영 안 함
    billed = {b.team_id: b.billed for b in db.scalars(select(TeamBilling).where(TeamBilling.event_id == ev.id))}
    assert sorted(billed.values()) == [55000, 60000]
    e005 = db.scalar(select(ChangeLog).where(ChangeLog.event_id == ev.id, ChangeLog.to_choice == "absent", ChangeLog.status == "applied"))
    assert e005.refund_amount == 10000 and e005.source == "chat"  # 납부 마감 후~행사 3일 전 50%

    r = up(client, f"{base}/transactions", transactions_csv())
    assert r.status_code == 200 and r.json()["inserted"] == 2
    client.post(f"{base}/agent/settlement-review")
    client.post(f"{base}/allocations/confirm-high")
    s = client.get(f"{base}/report").json()["summary"]
    assert (s["billed_total"], s["final_total"], s["received_total"]) == (115000, 105000, 115000)
    assert s["team_status"] == {"extra": 0, "refund": 1, "check": 1, "done": 0}  # 개발팀 환불 −10,000 · 경영팀 승인 대기
    assert s["refund_total"] == 10000


def test_reimporting_responses_resets_changes(db, client, demo):
    ev, base = demo
    up(client, f"{base}/roster", ROSTER)
    up(client, f"{base}/demo/responses", responses_csv())
    up(client, f"{base}/demo/changes", changes_csv())
    again = up(client, f"{base}/demo/responses", responses_csv()).json()
    assert again["changes"] == {"applied": 0, "pending": 0, "rejected": 0}
    assert again["counts"]["none"] == 1


def test_bad_rows_reject_whole_file_with_line_numbers(db, client, demo):
    ev, base = demo
    up(client, f"{base}/roster", ROSTER)
    bad = responses_csv().replace("E002,이나래", "E999,누구").replace("E005,정마루,개발팀,참석+버스", "E005,정마루,개발팀,버스만")
    r = up(client, f"{base}/demo/responses", bad)
    assert r.status_code == 422
    lines = {e["line"]: e["error"] for e in r.json()["error"]["detail"]}
    assert set(lines) == {3, 6} and "명단에 없는 ID" in lines[3] and "응답 값 오류" in lines[6]
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Response)) == 0  # 아무것도 반영 안 됨(명단 업로드 때 찍힌 전원 미응답 기준표 그대로)

    late = responses_csv().replace(kst(ATTEND - timedelta(hours=5)), kst(ATTEND + timedelta(hours=1)))
    r = up(client, f"{base}/demo/responses", late)
    assert r.status_code == 422 and "참석 마감" in r.json()["error"]["detail"][0]["error"]


def test_changes_need_responses_first_and_matching_from(db, client, demo):
    ev, base = demo
    up(client, f"{base}/roster", ROSTER)
    r = up(client, f"{base}/demo/changes", changes_csv())
    assert r.status_code == 422 and r.json()["error"]["code"] == "RESPONSES_FIRST"
    up(client, f"{base}/demo/responses", responses_csv())
    wrong = changes_csv().replace("E002,이나래,경영팀,참석+자차,불참", "E002,이나래,경영팀,참석+버스,불참")
    r = up(client, f"{base}/demo/changes", wrong)
    assert r.status_code == 422
    assert r.json()["error"]["detail"][0]["line"] == 5 and "변경 전" in r.json()["error"]["detail"][0]["error"]


def test_demo_upload_is_not_available_in_production(db, client, demo, monkeypatch):
    ev, base = demo
    monkeypatch.setattr(settings, "app_mode", "production")
    assert up(client, f"{base}/demo/responses", responses_csv()).status_code == 404
    assert up(client, f"{base}/demo/changes", changes_csv()).status_code == 404
