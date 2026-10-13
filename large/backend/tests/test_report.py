"""부서 명단 CSV · 마감 기준표 · 정산 보고서 · 총무 Agent."""
from __future__ import annotations

from datetime import timedelta

import pytest

from app import clock
from app.agents import admin_agent
from app.errors import AppError
from app.models import DevMail, EmailJob, Participant, RefundRule, SettlementReport, TeamBilling
from app.services import payments, report, responses, roster

from .conftest import NOW, make_world

HEAD = "ID,이름,전화번호,Slack ID,소속 부서,팀 담당자\n"


def csv(rows: list[str]) -> bytes:
    return (HEAD + "\n".join(rows) + "\n").encode()


# ---------------------------------------------------------------- 명단

def test_roster_groups_by_department_and_assigns_lead_by_id_or_name(db):
    w = make_world(db, n_teams=0)
    data = csv([
        "A01,김가나,010-1,UAAA00001,경영지원팀,A01",
        "A02,이다라,010-2,UAAA00002,경영지원팀,A01",
        "B01,박마바,010-3,UBBB00001,재무팀,박마바",
        "B02,최사아,,UBBB00002,재무팀,박마바",
        "C01,정자차,010-5,UCCC00001,인사팀,",
    ])
    out = roster.import_roster(db, w.event, data, "r.csv", f"admin:{w.admin.id}")
    assert out["rows"] == 5 and out["teams"] == 3 and out["managers_assigned"] == 2
    assert [(d["code"], d["name"], d["headcount"], d["manager"]) for d in out["departments"]] == [
        ("T01", "경영지원팀", 2, "김가나"), ("T02", "재무팀", 2, "박마바"), ("T03", "인사팀", 1, None),
    ]
    assert any("인사팀" in x for x in out["warnings"])  # 담당자 미지정은 경고
    # 다시 올려도 부서 코드·사람은 그대로(수정만)
    again = roster.import_roster(db, w.event, data, "r.csv", f"admin:{w.admin.id}")
    assert again["inserted"] == 0 and again["managers_assigned"] == 0


@pytest.mark.parametrize(
    "rows,msg",
    [
        (["A01,김가나,,UAAA00001,경영지원팀,A01", "A02,이다라,,UAAA00002,경영지원팀,A02"], "여러 명"),
        (["A01,김가나,,UAAA00001,경영지원팀,홍길동"], "명단에 없습니다"),
        (["A01,김가나,,UAAA00001,경영지원팀,김가나", "A02,김가나,,UAAA00002,경영지원팀,김가나"], "ID 로 적어"),
        (["A01,김가나,,not-slack,경영지원팀,A01"], "Slack ID 형식"),
        (["A01,김가나,,UAAA00001,경영지원팀,A01", "A01,이다라,,UAAA00002,경영지원팀,A01"], "중복"),
    ],
)
def test_roster_rejects_whole_file_on_error(db, rows, msg):
    w = make_world(db, n_teams=0)
    with pytest.raises(AppError) as e:
        roster.import_roster(db, w.event, csv(rows), "r.csv", f"admin:{w.admin.id}")
    assert msg in str(e.value.detail)
    assert db.query(Participant).count() == 0  # 아무것도 반영 안 함


def test_roster_requires_columns(db):
    w = make_world(db, n_teams=0)
    with pytest.raises(AppError) as e:
        roster.import_roster(db, w.event, b"ID,name,email\n1,a,b\n", "r.csv", f"admin:{w.admin.id}")
    assert e.value.code == "CSV_COLUMNS"


# ---------------------------------------------------------------- 기준표·보고서

def _pay_team(db, w, ti: int, amount: int, at):
    t = w.teams[ti]
    data = f"거래일시,입금자명,금액,메모\n{clock.kst(at).strftime('%Y-%m-%d %H:%M')},{w.members[ti][0].name},{amount},{t.code} 회비\n".encode()
    payments.import_transactions(db, w.event, data, f"tx{ti}{amount}.csv", "admin:1")
    for txn in db.query(payments.BankTransaction).filter_by(event_id=w.event.id).all():
        if payments.allocated_sum(db, txn.id) == 0 and txn.amount == amount:
            payments.allocate(db, w.event, txn.id, t.id, amount, status="confirmed", source="admin", basis="test", actor="admin:1", admin_id=w.admin.id)


def _world_after_event(db):
    """참석 마감 → 납부(전원 버스, 팀당 80,000) → 납부 마감 → 마감 후 변동 → 행사 종료."""
    w = make_world(db, n_teams=2, size=4)
    ev = w.event
    for ms in w.members.values():
        for p in ms:
            responses.submit_change(db, ev, p, "bus", source="web", actor="t")
    ev.attend_deadline, ev.payment_deadline, ev.event_at = NOW - timedelta(days=9), NOW - timedelta(days=5), NOW - timedelta(days=1)
    for r in db.query(RefundRule).filter_by(event_id=ev.id):  # 옮긴 마감에 맞춰 환불 규칙 기한도 옮김
        if r.valid_until is not None:
            r.valid_until = ev.payment_deadline if r.refund_percent == 100 else ev.event_at - timedelta(days=3)
    responses.ensure_snapshot(db, ev, ev.attend_deadline)
    _pay_team(db, w, 0, 80000, ev.payment_deadline - timedelta(days=1))
    _pay_team(db, w, 1, 80000, ev.payment_deadline - timedelta(days=1))
    report.ensure_billing(db, ev, ev.payment_deadline + timedelta(minutes=1))
    return w


def test_billing_is_frozen_at_payment_deadline(db):
    w = _world_after_event(db)
    b = db.query(TeamBilling).filter_by(team_id=w.teams[0].id).one()
    assert b.billed == 80000 and b.counts["bus"] == 4 and b.received_by_deadline == 80000
    assert report.ensure_billing(db, w.event) is False  # 두 번 저장하지 않음
    # 마감 후 변동이 생겨도 청구표는 그대로
    clock.freeze(NOW - timedelta(days=3))
    ch = responses.submit_change(db, w.event, w.members[0][1], "car", source="chat", actor="t")
    responses.decide_change(db, w.event, ch, w.admin, approve=True)
    clock.freeze(NOW)
    db.refresh(b)
    assert b.billed == 80000


def test_report_lists_refund_and_extra_people_and_contacts_manager(db):
    w = _world_after_event(db)
    ev = w.event
    # 팀1: 납부 마감 후 불참 → 50% 환불 10,000 / 팀2: 행사 당일 총무가 미응답자를 입력하는 대신, 버스→불참 규칙 없음(당일) → 확인 필요
    clock.freeze(ev.payment_deadline + timedelta(hours=1))
    ch = responses.submit_change(db, ev, w.members[0][2], "absent", source="chat", actor="t")
    responses.decide_change(db, ev, ch, w.admin, approve=True)
    clock.freeze(ev.event_at + timedelta(hours=1))
    pending = responses.submit_change(db, ev, w.members[1][3], "absent", source="admin", actor="admin:1", note="당일 불참")
    clock.freeze(NOW)
    rep = report.refresh(db, ev, "test")
    t1, t2 = rep.teams
    assert (t1["billed"], t1["final_fee"], t1["received"], t1["balance"], t1["status"]) == (80000, 70000, 80000, -10000, "refund")
    assert t1["manager"]["name"] == w.members[0][0].name  # 환불은 담당자에게
    assert t2["status"] == "check" and any("승인 대기" in b for b in t2["blockers"])
    kinds = {(p["name"], p["kind"], p["amount"]) for p in rep.people}
    assert (w.members[0][2].name, "refund", 10000) in kinds
    assert (w.members[1][3].name, "pending", 0) in kinds
    assert next(p for p in rep.people if p["kind"] == "pending")["on_event_day"] is True
    assert rep.summary["refund_total"] == 10000 and rep.summary["team_status"]["check"] == 1
    # 내용이 같으면 새 버전 없음, 바뀌면 diff 와 함께 새 버전
    assert report.refresh(db, ev, "again").version == rep.version
    responses.decide_change(db, ev, pending, w.admin, approve=True, refund_amount=0, note="당일 불참: 버스·식사 확정")
    rep2 = report.refresh(db, ev, "approved")
    assert rep2.version == rep.version + 1
    assert [d["name"] for d in rep2.diff["teams"]] == [w.teams[1].name]
    assert db.query(SettlementReport).count() == 2


def test_csv_export_has_team_and_people_rows(db):
    w = _world_after_event(db)
    rep = report.refresh(db, w.event, "t")
    text = report.to_csv(rep)
    assert "청구액" in text and w.teams[0].name in text


# ---------------------------------------------------------------- 총무 Agent

def _admin_world(db):
    w = _world_after_event(db)
    w.teams[0].name = "경영지원팀"
    clock.freeze(w.event.payment_deadline + timedelta(hours=1))
    ch = responses.submit_change(db, w.event, w.members[0][1], "absent", source="chat", actor="t")
    responses.decide_change(db, w.event, ch, w.admin, approve=True)
    clock.freeze(NOW)
    db.flush()
    return w


def test_admin_agent_answers_department_question_from_report(db):
    w = _admin_world(db)
    out = admin_agent.handle_message(db, w.event, "경영지원팀의 환불 대상자를 알려줘")
    text = out["reply"]["text"]
    assert "경영지원팀" in text and w.members[0][1].name in text and "10,000원" in text
    assert "담당자" in text  # 회비는 팀 단위 → 담당자에게 안내
    assert out["reply"]["tools"][0]["tool"] == "list_people"


def test_admin_agent_notify_requires_confirmation_and_goes_to_manager_only(db):
    w = _admin_world(db)
    first = admin_agent.handle_message(db, w.event, "환불 대상 담당자에게 안내 보내줘")
    assert first["chat"]["state"] == "awaiting_confirmation"
    assert db.query(EmailJob).filter_by(kind="refund_notice").count() == 0  # 확인 전에는 아무것도 안 만듦
    done = admin_agent.handle_message(db, w.event, "네")
    assert done["chat"]["state"] == "idle"
    jobs = db.query(EmailJob).filter_by(kind="refund_notice").all()
    assert len(jobs) == 1 and jobs[0].participant_id == w.members[0][0].id  # 담당자만
    mail = db.query(DevMail).filter_by(email_job_id=jobs[0].id).one()
    assert mail.to_email == w.members[0][0].slack_id and "10,000원" in mail.body
    # 같은 금액이면 다시 보내도 중복 작업 없음
    admin_agent.handle_message(db, w.event, "환불 대상 담당자에게 안내 보내줘")
    admin_agent.handle_message(db, w.event, "네")
    assert db.query(EmailJob).filter_by(kind="refund_notice").count() == 1


def test_admin_agent_cancel_and_unknown_department(db):
    w = _admin_world(db)
    admin_agent.handle_message(db, w.event, "환불 안내 보내줘")
    assert admin_agent.handle_message(db, w.event, "아니요")["reply"]["text"].startswith("알겠어요")
    assert db.query(EmailJob).filter_by(kind="refund_notice").count() == 0
    out = admin_agent.handle_message(db, w.event, "T09 현황")
    assert "찾을 수 없" in out["reply"]["text"]


def test_report_api_and_admin_chat_endpoint(client, db):
    w = _admin_world(db)
    db.commit()
    client.post("/api/large/auth/admin/login", json={"email": "admin@example.com", "password": "pw-123456"})
    base = f"/api/large/admin/events/{w.event.id}"
    r = client.get(f"{base}/report").json()
    assert r["summary"]["stage"] == "event_done" and r["teams"][0]["status"] == "refund"
    assert client.get(f"{base}/report.csv").status_code == 200
    chat = client.post(f"{base}/agent/chat", json={"text": "전체 요약"}).json()
    assert "정산 보고서" in chat["reply"]["text"]
    assert client.get(f"{base}/agent/chat").json()["messages"][-1]["sender"] == "agent"
