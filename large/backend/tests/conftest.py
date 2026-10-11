from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import clock, db as dbmod
from app.config import settings
from app.models import Admin, Event, Participant, RefundRule, Team, TeamManager
from app.security import hash_password

NOW = datetime(2026, 10, 8, 2, 0)  # naive UTC = KST 11:00 (발송 허용 시간)
HEADERS = {"X-Requested-With": "chongmu"}


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_mode", "demo")
    monkeypatch.setattr(settings, "email_mode", "mock")
    monkeypatch.setattr(settings, "llm_mode", "mock")
    dbmod.configure(f"sqlite:///{tmp_path / 'test.db'}")
    dbmod.init_db()
    clock.freeze(NOW)
    yield
    clock.freeze(None)


@pytest.fixture
def db():
    s = dbmod.SessionLocal()
    yield s
    s.close()


@dataclass
class World:
    admin: Admin
    event: Event
    teams: list[Team]
    members: dict[int, list[Participant]]  # team index -> participants


def make_world(db, *, n_teams=3, size=4, after_attend=False, after_payment=False, email="admin@example.com", prefix="") -> World:
    admin = db.query(Admin).filter_by(email=email).one_or_none()
    if admin is None:
        admin = Admin(email=email, password_hash="")
        db.add(admin)
    admin.password_hash = hash_password("pw-123456")
    db.flush()
    attend = NOW - timedelta(days=2) if after_attend else NOW + timedelta(days=2)
    payment = NOW - timedelta(hours=1) if after_payment else NOW + timedelta(days=3)
    ev = Event(admin_id=admin.id, name=f"{prefix}테스트 워크숍", event_at=NOW + timedelta(days=10), attend_deadline=attend, payment_deadline=payment, fee_bus=20000, fee_car=15000, fee_absent=0)
    db.add(ev)
    db.flush()
    db.add_all([
        RefundRule(event_id=ev.id, change_type="attend_to_absent", valid_until=payment, refund_percent=100, description="입금 마감 전 불참 100%"),
        RefundRule(event_id=ev.id, change_type="attend_to_absent", valid_until=ev.event_at - timedelta(days=3), refund_percent=50, description="행사 3일 전까지 50%"),
        RefundRule(event_id=ev.id, change_type="bus_to_car", valid_until=payment, refund_percent=100, description="입금 마감 전 버스→자차 100%"),
        RefundRule(event_id=ev.id, change_type="bus_to_car", valid_until=None, refund_percent=0, description="입금 마감 후 버스→자차 0%"),
    ])
    teams, members = [], {}
    n = 0
    for ti in range(n_teams):
        t = Team(event_id=ev.id, code=f"T{ti + 1:02d}", name=f"{prefix}팀{ti + 1}")
        db.add(t)
        db.flush()
        teams.append(t)
        members[ti] = []
        for mi in range(size):
            n += 1
            p = Participant(event_id=ev.id, team_id=t.id, employee_no=f"{prefix}E{n:03d}", name=f"{prefix}이" + chr(0xAC00 + n * 37) + chr(0xAC00 + n * 53 + 11), email=f"{prefix}p{n:03d}@example.com")
            db.add(p)
            db.flush()
            members[ti].append(p)
        db.add(TeamManager(team_id=t.id, participant_id=members[ti][0].id, assigned_by=admin.id))
    db.commit()
    return World(admin, ev, teams, members)


@pytest.fixture
def client():
    from app.main import create_app

    with TestClient(create_app()) as c:
        c.headers.update(HEADERS)
        yield c
