"""이번 통합 단계에서 확인한 위험: 모호한 '네', 환불 규칙 부재의 임시 0원, SMALL/LARGE 행사 혼선."""
from datetime import timedelta

import pytest

from app import clock, db as dbmod
from app.agents import communication
from app.agents.settlement_agent import Run, ask_manager
from app.errors import AppError
from app.models import ChangeLog, ConfirmTask, RefundRule, Response
from app.services import invites, responses, settlement
from app.services.common import create_task

from .conftest import NOW, make_world


def say(db, w, p, text, role="manager"):
    out = communication.handle_message(db, w.event, p, role, text)
    db.commit()
    return out


def _two_tasks(db, w):
    team, mgr = w.teams[0], w.members[0][0]
    run = Run(db, w.event, "test")
    yes_no = [{"key": "yes", "label": "네"}, {"key": "no", "label": "아니요"}]
    a = ask_manager(run, team=team, kind="possible_duplicate", dedupe_key="a", question="A 입금이 우리 팀 것인가요?", options=yes_no, rule="t", evidence={})
    b = ask_manager(run, team=team, kind="possible_duplicate", dedupe_key="b", question="B 입금이 우리 팀 것인가요?", options=yes_no, rule="t", evidence={})
    db.commit()
    return mgr, a, b


# ---------------------------------------------------------------- 1. 여러 확인 과제가 열린 상태의 '네'

def test_bare_yes_with_several_open_tasks_answers_nothing(db):
    w = make_world(db)
    mgr, a, b = _two_tasks(db, w)
    out = say(db, w, mgr, "네")
    assert out["thread"]["state"] == "awaiting_info"  # 어느 과제인지 되묻는다
    out = say(db, w, mgr, "네")
    assert out["thread"]["state"] == "awaiting_info"
    assert {t.status for t in db.query(ConfirmTask).filter(ConfirmTask.id.in_([a.id, b.id]))} == {"open"}


def test_yes_after_new_notice_reconfirms_instead_of_executing(db):
    w = make_world(db)
    mgr, a, b = _two_tasks(db, w)
    out = say(db, w, mgr, f"#{a.id} 네")
    assert out["thread"]["state"] == "awaiting_confirmation"
    # 확인 질문 뒤에 정산 Agent 가 새 확인 요청(C)을 같은 대화창에 올림
    run = Run(db, w.event, "test")
    c = ask_manager(run, team=w.teams[0], kind="possible_duplicate", dedupe_key="c", question="C 입금?", options=a.options, rule="t", evidence={})
    db.commit()
    out = say(db, w, mgr, "네")  # C 를 보고 한 '네'일 수 있음
    assert "다시 확인" in out["reply"]["text"] and f"#{a.id}" in out["reply"]["text"]
    assert db.get(ConfirmTask, a.id).status == "open" and db.get(ConfirmTask, c.id).status == "open"
    say(db, w, mgr, "네")  # 다시 물은 질문에 대한 '네' → A 만 답변
    db.expire_all()
    assert db.get(ConfirmTask, a.id).status != "open"
    assert db.get(ConfirmTask, b.id).status == "open" and db.get(ConfirmTask, c.id).status == "open"


def test_yes_after_task_closed_elsewhere_does_not_execute(db):
    w = make_world(db)
    mgr, a, b = _two_tasks(db, w)
    say(db, w, mgr, f"#{a.id} 아니요")
    a_row = db.get(ConfirmTask, a.id)
    a_row.status = "resolved"  # 그사이 총무가 직접 처리
    db.commit()
    out = say(db, w, mgr, "네")
    assert out["thread"]["state"] == "idle" and "처리하지 못했" in out["reply"]["text"]
    assert db.get(ConfirmTask, a.id).answer is None


# ---------------------------------------------------------------- 2. 환불 규칙 부재 → 임시 0원

def _rule_missing_change(db):
    w = make_world(db, size=2)
    p = w.members[0][1]
    clock.freeze(NOW - timedelta(days=5))
    responses.submit_change(db, w.event, p, "bus", source="web", actor="t")
    responses.submit_change(db, w.event, w.members[0][0], "bus", source="web", actor="t")
    clock.freeze(w.event.event_at - timedelta(days=1))  # 행사 하루 전: 불참 환불 규칙(3일 전까지)이 없음
    ch = responses.submit_change(db, w.event, p, "absent", source="chat", actor="t")
    db.commit()
    return w, p, ch


def test_rule_missing_change_is_not_a_zero_refund_decision(db):
    w, p, ch = _rule_missing_change(db)
    assert ch.status == "pending_approval" and ch.rule_missing and ch.refund_amount == 0
    task = db.query(ConfirmTask).filter_by(change_id=ch.id).one()
    assert "미정" in task.question and task.status == "open"
    with pytest.raises(AppError) as e:
        responses.decide_change(db, w.event, ch, w.admin, approve=True)
    assert e.value.code == "REFUND_RULE_MISSING"
    db.rollback()
    with pytest.raises(AppError) as e:
        settlement.confirm(db, w.event, w.teams[0].id, w.admin)
    assert e.value.code == "SETTLEMENT_BLOCKED"
    db.rollback()
    assert db.query(Response).filter_by(participant_id=p.id).one().choice == "bus"  # 아직 반영 안 됨


def test_admin_must_state_amount_and_reason(db):
    w, p, ch = _rule_missing_change(db)
    with pytest.raises(AppError) as e:
        responses.decide_change(db, w.event, ch, w.admin, approve=True, refund_amount=5000)
    assert e.value.code == "NOTE_REQUIRED"
    db.rollback()
    with pytest.raises(AppError) as e:
        responses.decide_change(db, w.event, db.get(ChangeLog, ch.id), w.admin, approve=True, refund_amount=20001, note="x")
    assert e.value.code == "INVALID_REFUND"
    db.rollback()
    ch = responses.decide_change(db, w.event, db.get(ChangeLog, ch.id), w.admin, approve=True, refund_amount=5000, note="숙소 위약금 제외 후 환불")
    db.commit()
    assert ch.status == "applied" and ch.admin_refund_decided and ch.refund_amount == 5000 and ch.liability_after == 15000
    calc = settlement.compute_team(db, w.event, w.teams[0])
    assert not any("임시 0원" in b for b in calc["blockers"])


def test_adding_rule_then_approving_uses_rule(db):
    w, p, ch = _rule_missing_change(db)
    db.add(RefundRule(event_id=w.event.id, change_type="attend_to_absent", valid_until=w.event.event_at, refund_percent=30, description="전날 30%"))
    db.commit()
    ch = responses.decide_change(db, w.event, ch, w.admin, approve=True)
    assert ch.refund_amount == 6000 and not ch.rule_missing


def test_legacy_applied_zero_refund_blocks_settlement(db):
    w, p, ch = _rule_missing_change(db)
    ch.status = "applied"  # 이전 버전 데이터처럼 규칙 없이 0원으로 반영된 건
    db.query(ConfirmTask).filter_by(change_id=ch.id).one().status = "resolved"
    db.commit()
    calc = settlement.compute_team(db, w.event, w.teams[0])
    assert any("임시 0원" in b for b in calc["blockers"])


def test_chat_confirmation_says_refund_undecided(db):
    w = make_world(db, size=1)
    p = w.members[0][0]
    clock.freeze(NOW - timedelta(days=5))
    responses.submit_change(db, w.event, p, "bus", source="web", actor="t")
    clock.freeze(w.event.event_at - timedelta(days=1))
    db.commit()
    out = say(db, w, p, "불참할게요", role="participant")
    assert "미정" in out["reply"]["text"] and "0원으로 확정된 것이 아님" in out["reply"]["text"]


# ---------------------------------------------------------------- 3. 행사 모드(small/large)

def test_small_mode_event_is_invisible_to_large_api(client, db):
    w = make_world(db)
    w.event.mode = "small"
    token = invites.issue(db, w.members[0][1])
    db.commit()
    client.post("/api/large/auth/admin/login", json={"email": "admin@example.com", "password": "pw-123456"})
    assert client.get(f"/api/large/admin/events/{w.event.id}").status_code == 404
    assert client.post("/api/large/auth/invite/exchange", json={"token": token}).status_code == 401
    r = client.post("/api/large/auth/manager/request-code", json={"email": w.members[0][0].email})
    assert r.status_code == 200  # 응답은 같지만
    from app.models import DevMail
    assert db.query(DevMail).count() == 0  # 코드는 보내지 않음


def test_mode_cannot_change(client, db):
    w = make_world(db)
    client.post("/api/large/auth/admin/login", json={"email": "admin@example.com", "password": "pw-123456"})
    r = client.put(f"/api/large/admin/events/{w.event.id}", json={"mode": "small"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "MODE_LOCKED"
    assert client.put(f"/api/large/admin/events/{w.event.id}", json={"mode": "large", "name": "새 이름"}).status_code == 200


def test_old_db_gets_new_columns(db):
    with dbmod.engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE events DROP COLUMN mode")
        conn.exec_driver_sql("ALTER TABLE change_logs DROP COLUMN rule_missing")
    dbmod.init_db()
    with dbmod.engine.begin() as conn:
        assert "mode" in {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(events)")}
        assert "rule_missing" in {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(change_logs)")}
