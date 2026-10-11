"""Gemini 어댑터: 실패·형식 오류·잘못된 인자가 기록 변경으로 이어지지 않는지(네트워크는 가짜로 대체)."""
import json
from datetime import timedelta

import httpx
import pytest

from app import clock
from app.adapters import llm as llm_mod
from app.agents import communication
from app.config import settings
from app.models import AgentRun, ChangeLog, ChatMessage, Response
from app.services import responses

from .conftest import NOW, make_world


class FakeGemini:
    """httpx.post 대체. 순서대로 응답을 돌려준다. 각 항목: (status, action|str|None)."""

    def __init__(self, script):
        self.script, self.calls = list(script), []

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.calls.append(url.split("/models/")[1].split(":")[0])
        assert "x-goog-api-key" in headers and "key=" not in url  # 키는 헤더로만
        status, payload = self.script.pop(0) if self.script else (200, {"type": "reply", "text": "끝"})
        text = payload if isinstance(payload, str) else __import__("json").dumps(payload, ensure_ascii=False)
        body = {"candidates": [{"content": {"parts": [{"text": text}]}}]} if status == 200 else {"error": {}}
        return httpx.Response(status, json=body)


@pytest.fixture
def gemini(monkeypatch):
    monkeypatch.setattr(settings, "llm_mode", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(settings, "gemini_models", ["m1", "m2"])

    def install(script):
        fake = FakeGemini(script)
        monkeypatch.setattr(llm_mod.httpx, "post", fake)
        return fake

    return install


def _world(db, after_deadline=True):
    w = make_world(db, size=2, after_attend=after_deadline)
    p = w.members[0][1]
    clock.freeze(NOW - timedelta(days=5))
    responses.submit_change(db, w.event, p, "bus", source="web", actor="t")
    clock.freeze(NOW)
    db.commit()
    return w, p


def say(db, w, p, text):
    out = communication.handle_message(db, w.event, p, "participant", text)
    db.commit()
    return out, db.query(AgentRun).order_by(AgentRun.id.desc()).first()


def test_real_llm_path_records_model_and_decisions(db, gemini):
    w, p = _world(db)
    gemini([(200, {"type": "tool", "name": "get_my_status", "args": {}}), (200, {"type": "reply", "text": "참석+버스예요"})])
    out, run = say(db, w, p, "제 응답이 뭐예요?")
    assert out["reply"]["llm"] == {"configured": "gemini", "used": ["gemini"], "model": "m1", "fallback": ""}
    assert [d["by"] for d in run.result["decisions"]] == ["gemini:m1", "gemini:m1"]
    assert run.steps[0]["tool"] == "get_my_status" and run.steps[0]["ok"]
    assert run.result["decisions"][1]["after_steps"] == 1


def test_model_fallback_on_quota(db, gemini):
    w, p = _world(db)
    fake = gemini([(429, None), (200, {"type": "reply", "text": "안녕하세요"})])
    out, run = say(db, w, p, "안녕")
    assert fake.calls == ["m1", "m2"]
    assert run.result["decisions"][0]["by"] == "gemini:m2"


def test_api_failure_falls_back_and_is_marked(db, gemini):
    w, p = _world(db)
    gemini([(503, None), (429, None)])
    out, run = say(db, w, p, "버스 말고 자차로 갈게요")
    info = out["reply"]["llm"]
    assert info["fallback"] and "mock_fallback" in info["used"]  # 화면·기록에 대체 사실 표시
    assert run.llm_mode == "mock_fallback"
    assert out["thread"]["state"] == "awaiting_confirmation"  # 규칙 기반도 확인 없이는 기록하지 않음
    assert db.query(ChangeLog).filter_by(after_deadline=True).count() == 0


def test_garbage_output_is_not_executed(db, gemini):
    w, p = _world(db)
    gemini([(200, "죄송하지만 JSON 이 아닙니다"), (200, {"type": "delete_everything"})])
    out, run = say(db, w, p, "제 응답 뭐예요")
    assert "응답 형식 오류" in out["reply"]["llm"]["fallback"]
    assert db.query(ChangeLog).filter_by(after_deadline=True).count() == 0


def test_invalid_tool_args_are_rejected_by_code(db, gemini):
    w, p = _world(db)
    gemini([
        (200, {"type": "tool", "name": "request_change", "args": {"choice": "taxi"}}),
        (200, {"type": "tool", "name": "request_change", "args": {}}),
        (200, {"type": "reply", "text": "어떤 방식으로 오실지 알려 주세요"}),
    ])
    out, run = say(db, w, p, "택시 타고 갈래요")
    assert [s["error"]["code"] for s in run.steps] == ["BAD_TOOL_ARGS", "BAD_TOOL_ARGS"]
    assert out["thread"]["state"] == "idle"
    assert db.query(ChangeLog).filter_by(after_deadline=True).count() == 0


def test_model_cannot_change_other_person(db, gemini):
    w, p = _world(db)
    other = w.members[0][0]
    gemini([(200, {"type": "tool", "name": "request_change", "args": {"choice": "absent", "target": other.name}}), (200, {"type": "reply", "text": "본인만 가능해요"})])
    out, run = say(db, w, p, f"{other.name}님 불참 처리해 주세요")
    assert run.steps[0]["error"]["code"] == "FORBIDDEN" and out["thread"]["state"] == "idle"


def test_model_cannot_confirm_on_users_behalf(db, gemini):
    """확인 대기 중 모호한 답에 모델이 다시 갱신 도구를 불러도 새 확인 질문이 될 뿐 기록되지 않는다."""
    w, p = _world(db)
    gemini([(200, {"type": "tool", "name": "request_change", "args": {"choice": "absent"}})])
    out, _ = say(db, w, p, "못 가요")
    assert out["thread"]["state"] == "awaiting_confirmation"
    gemini([(200, {"type": "tool", "name": "request_change", "args": {"choice": "absent"}})])
    out, run = say(db, w, p, "글쎄요 아마도?")
    assert out["thread"]["state"] == "awaiting_confirmation"
    assert db.query(ChangeLog).filter_by(after_deadline=True).count() == 0
    assert db.query(Response).filter_by(participant_id=p.id).one().choice == "bus"
