"""소통 Agent(대화 상태·확인·권한)와 정산 Agent(확인 과제·재검토)."""
from datetime import timedelta

from app import clock
from app.agents import communication
from app.agents.settlement_agent import review_event
from app.models import AgentRun, Allocation, ChangeLog, ChatThread, ConfirmTask, Response
from app.services import payments, responses

from .conftest import NOW, make_world


def say(db, w, p, text, role="participant"):
    out = communication.handle_message(db, w.event, p, role, text)
    db.commit()
    return out


def test_change_needs_explicit_confirmation(db):
    w = make_world(db)
    p = w.members[0][1]
    responses.submit_change(db, w.event, p, "bus", source="web", actor="t")
    db.commit()
    out = say(db, w, p, "버스 말고 자차로 갈게요")
    assert out["thread"]["state"] == "awaiting_confirmation"
    assert "15,000원" in out["reply"]["text"]  # 금액은 코드가 계산한 값
    assert db.query(Response).filter_by(participant_id=p.id).one().choice == "bus"  # 아직 안 바뀜
    out = say(db, w, p, "네")
    assert out["thread"]["state"] == "idle"
    assert db.query(Response).filter_by(participant_id=p.id).one().choice == "car"
    change = db.query(ChangeLog).order_by(ChangeLog.id.desc()).first()
    assert change.source == "chat" and change.evidence_message_id is not None  # 근거 메시지 연결


def test_missing_info_question_persists_and_can_be_cancelled(db):
    w = make_world(db)
    p = w.members[0][1]
    out = say(db, w, p, "응답을 변경하고 싶어요")
    assert out["thread"]["state"] == "awaiting_info"
    th = db.query(ChatThread).one()
    assert th.pending == {"intent": "change"}  # 나중에 이어서 답할 수 있게 저장
    out = say(db, w, p, "불참이요")
    assert out["thread"]["state"] == "awaiting_confirmation"
    out = say(db, w, p, "아니요")
    assert out["thread"]["state"] == "idle" and db.query(Response).count() == 0


def test_cannot_change_someone_else(db):
    w = make_world(db)
    p, other = w.members[0][1], w.members[0][2]
    out = say(db, w, p, f"{other.name}님 불참으로 바꿔 주세요")
    assert "본인" in out["reply"]["text"]
    assert out["reply"]["tools"][0]["ok"] is False
    assert db.query(ChangeLog).count() == 0


def test_after_deadline_change_goes_to_admin_approval(db):
    w = make_world(db, size=2)
    p = w.members[0][1]
    clock.freeze(NOW - timedelta(days=3))
    responses.submit_change(db, w.event, p, "bus", source="web", actor="t")
    clock.freeze(NOW + timedelta(days=3, hours=1))  # 참석 마감 후, 입금 마감 후
    db.commit()
    out = say(db, w, p, "못 가게 됐어요 불참할게요")
    assert "50%" in out["reply"]["text"] or "10,000원" in out["reply"]["text"]
    say(db, w, p, "네")
    ch = db.query(ChangeLog).filter_by(status="pending_approval").one()
    assert ch.refund_amount == 10000
    assert db.query(ConfirmTask).filter_by(kind="change_approval", change_id=ch.id).one().assignee == "admin"
    assert db.query(Response).filter_by(participant_id=p.id).one().choice == "bus"  # 승인 전엔 그대로


class EagerLLM:
    """확인 없이 바로 갱신 도구를 부르는 LLM. 코드가 막아야 한다."""

    mode = "fake"

    def __init__(self, action):
        self.action = action

    def decide(self, inp):
        if inp.steps:
            return {"type": "reply", "text": "done"}
        return self.action


def test_llm_cannot_skip_confirmation(db, monkeypatch):
    w = make_world(db)
    p = w.members[0][1]
    monkeypatch.setattr(communication, "get_llm", lambda: EagerLLM({"type": "tool", "name": "request_change", "args": {"choice": "absent"}}))
    out = say(db, w, p, "아무 말")
    assert out["thread"]["state"] == "awaiting_confirmation"
    assert db.query(ChangeLog).count() == 0


def test_llm_cannot_use_tools_of_other_roles_or_targets(db, monkeypatch):
    w = make_world(db)
    p = w.members[0][1]
    monkeypatch.setattr(communication, "get_llm", lambda: EagerLLM({"type": "tool", "name": "list_open_tasks", "args": {}}))
    out = say(db, w, p, "아무 말")
    run = db.query(AgentRun).order_by(AgentRun.id.desc()).first()
    # 역할에 없는 도구는 실행 전에 거절되고, 이 턴은 규칙 기반으로 대체됐다고 기록된다
    assert "없는 도구" in run.result["llm"]["fallback"]
    assert run.result["decisions"][0]["by"] == "mock_fallback"
    assert all(s["tool"] != "list_open_tasks" for s in run.steps)
    monkeypatch.setattr(communication, "get_llm", lambda: EagerLLM({"type": "tool", "name": "request_change", "args": {"choice": "absent", "target": w.members[1][1].name}}))
    out = say(db, w, p, "아무 말")
    assert out["thread"]["state"] == "idle" and db.query(ChangeLog).count() == 0


def _ambiguous_setup(db):
    w = make_world(db, n_teams=2, size=2)
    a, b = w.members[0][1], w.members[1][1]
    b.name = a.name  # 동명이인
    for ps in w.members.values():
        for p in ps:
            responses.submit_change(db, w.event, p, "bus", source="web", actor="t")
    payments.import_transactions(db, w.event, f"거래일시,입금자명,금액,메모\n2026-10-07 10:00,{a.name},20000,\n".encode(), "t.csv", "t")
    db.commit()
    review_event(db, w.event)
    db.commit()
    tasks = db.query(ConfirmTask).filter_by(kind="ambiguous_depositor").order_by(ConfirmTask.team_id).all()
    assert len(tasks) == 2
    return w, tasks


def test_manager_answers_resolve_ambiguous_deposit(db):
    w, (t1, t2) = _ambiguous_setup(db)
    m1, m2 = w.members[0][0], w.members[1][0]
    out = say(db, w, m1, f"#{t1.id} 네 맞아요", role="manager")
    assert out["thread"]["state"] == "awaiting_confirmation"
    out = say(db, w, m1, "네", role="manager")
    assert "기다리" in out["reply"]["text"]  # 다른 후보 팀 답변 대기
    assert db.query(Allocation).count() == 0
    say(db, w, m2, f"#{t2.id} 아니요", role="manager")
    say(db, w, m2, "네", role="manager")
    a = db.query(Allocation).one()
    assert a.team_id == w.teams[0].id and a.status == "proposed" and a.participant_id == t1.participant_id
    assert {t.status for t in db.query(ConfirmTask).filter_by(kind="ambiguous_depositor")} == {"resolved"}


def test_all_no_answers_escalate_to_admin(db):
    w, (t1, t2) = _ambiguous_setup(db)
    for m, t in ((w.members[0][0], t1), (w.members[1][0], t2)):
        say(db, w, m, f"#{t.id} 아니요", role="manager")
        say(db, w, m, "네", role="manager")
    assert db.query(Allocation).count() == 0
    esc = db.query(ConfirmTask).filter_by(kind="deposit_unresolved").one()
    assert esc.assignee == "admin" and esc.status == "escalated"


def test_manager_cannot_answer_other_team_task(db):
    w, (t1, t2) = _ambiguous_setup(db)
    out = say(db, w, w.members[0][0], f"#{t2.id} 네", role="manager")
    assert out["thread"]["state"] == "idle"
    assert db.query(ConfirmTask).get(t2.id).status == "open"


def test_agent_runs_record_steps_and_rules(db):
    w = make_world(db)
    p = w.members[0][1]
    responses.submit_change(db, w.event, p, "bus", source="web", actor="t")
    db.commit()
    say(db, w, p, "자차로 바꿀게요")
    run = db.query(AgentRun).filter_by(agent="communication").order_by(AgentRun.id.desc()).first()
    assert run.evidence_message_id is not None and run.steps and run.rules


def test_presentation_dialogue_mock(db):
    """발표 주 장면 대사(모의 LLM): 모호한 요청 → 되묻기 → 재개 → 코드 금액 확인 → 네 → 총무 승인 대기."""
    w = make_world(db, size=2)
    p = w.members[0][1]
    clock.freeze(NOW - timedelta(days=3))
    responses.submit_change(db, w.event, p, "bus", source="web", actor="t")
    clock.freeze(NOW + timedelta(days=3, hours=2))  # 참석·입금 마감 후
    db.commit()
    out = say(db, w, p, "저 이번에 버스는 못 탈 것 같아요")
    assert out["thread"]["state"] == "awaiting_info" and "자차" in out["reply"]["text"] and "불참" in out["reply"]["text"]
    out = say(db, w, p, "아예 못 가요")
    assert out["thread"]["state"] == "awaiting_confirmation" and "10,000원" in out["reply"]["text"]
    say(db, w, p, "네")
    ch = db.query(ChangeLog).filter_by(after_deadline=True).one()
    assert (ch.to_choice, ch.status, ch.refund_amount) == ("absent", "pending_approval", 10000)
