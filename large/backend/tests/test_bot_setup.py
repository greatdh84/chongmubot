"""초기설정 화면(워크샵) 연동: 총무봇 성격·전략 저장, meta 의 DB 종류, 빈 행사 만들기."""
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app import db as dbmod
from app.models import DbMeta, Event, Participant, RefundRule

from .conftest import make_world

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "make_empty_event.py"


def _admin_login(client):
    r = client.post("/api/large/auth/admin/login", json={"email": "admin@example.com", "password": "pw-123456"})
    assert r.status_code == 200


def test_bot_settings_saved_and_validated(db, client):
    w = make_world(db)
    _admin_login(client)
    base = f"/api/large/admin/events/{w.event.id}"
    assert client.get("/api/large/admin/events").json()[0]["bot"] == {"persona": None, "strategy": None}
    r = client.post(f"{base}/bot", json={"persona": "oldie", "strategy": "escalate"})
    assert r.status_code == 200 and r.json()["bot"] == {"persona": "oldie", "strategy": "escalate"}
    assert client.get("/api/large/admin/events").json()[0]["bot"]["persona"] == "oldie"
    assert client.post(f"{base}/bot", json={"persona": "villain", "strategy": "escalate"}).status_code == 422


def test_bot_settings_need_admin(db, client):
    w = make_world(db)
    assert client.post(f"/api/large/admin/events/{w.event.id}/bot", json={"persona": "oldie", "strategy": "escalate"}).status_code == 401


def test_meta_reports_db_purpose(db, client):
    assert client.get("/api/large/meta").json()["db_purpose"] is None
    db.merge(DbMeta(key="purpose", value="demo-seed"))
    db.commit()
    assert client.get("/api/large/meta").json()["db_purpose"] == "demo-seed"


def _run_empty_event():
    spec = importlib.util.spec_from_file_location("make_empty_event", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.main()


def test_make_empty_event_creates_one_event_and_refuses_second(db):
    _run_empty_event()
    with dbmod.SessionLocal() as s:
        ev = s.scalars(select(Event)).one()
        assert ev.attend_deadline < ev.payment_deadline < ev.event_at
        assert s.scalar(select(func.count()).select_from(Participant)) == 0
        assert s.scalar(select(func.count()).select_from(RefundRule)) == 3
        assert s.get(DbMeta, "purpose").value == "demo-live"
    with pytest.raises(SystemExit, match="이미 행사가"):
        _run_empty_event()
