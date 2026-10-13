"""초대 토큰·세션·역할별 접근 제한."""
import logging
import re
from datetime import timedelta

import pytest

from app import clock
from app.config import Settings
from app.models import AuthSession, DevMail, Invite, Response, TeamManager
from app.security import RedactSecretsFilter, hash_token
from app.services import invites

from .conftest import NOW, make_world


def login_participant(client, db, p):
    token = invites.issue(db, p)
    db.commit()
    r = client.post("/api/large/auth/invite/exchange", json={"token": token})
    assert r.status_code == 200, r.text
    return token


def login_admin(client, email="admin@example.com"):
    r = client.post("/api/large/auth/admin/login", json={"email": email, "password": "pw-123456"})
    assert r.status_code == 200, r.text


def test_invite_token_stored_only_as_hash(client, db):
    w = make_world(db)
    token = login_participant(client, db, w.members[0][1])
    inv = db.query(Invite).one()
    assert inv.token_hash == hash_token(token) and token not in inv.token_hash
    assert token not in client.get("/api/large/me").text  # 응답에도 토큰을 돌려주지 않음


def test_invite_expired_revoked_invalid(client, db):
    w = make_world(db)
    p = w.members[0][1]
    token = invites.issue(db, p)
    db.commit()
    clock.freeze(NOW + timedelta(days=31))
    assert client.post("/api/large/auth/invite/exchange", json={"token": token}).json()["error"]["code"] == "INVITE_EXPIRED"
    clock.freeze(NOW)
    invites.revoke_all(db, p.id)
    db.commit()
    assert client.post("/api/large/auth/invite/exchange", json={"token": token}).json()["error"]["code"] == "INVITE_REVOKED"
    assert client.post("/api/large/auth/invite/exchange", json={"token": "x" * 43}).json()["error"]["code"] == "INVITE_INVALID"


def test_reissue_revokes_old_links(client, db):
    w = make_world(db)
    p = w.members[0][1]
    old = invites.issue(db, p)
    db.commit()
    login_admin(client)
    r = client.post(f"/api/large/admin/events/{w.event.id}/participants/{p.id}/invite/reissue")
    assert r.status_code == 200 and r.json()["revoked"] == 1
    assert client.post("/api/large/auth/invite/exchange", json={"token": old}).status_code == 401
    body = db.query(DevMail).filter_by(to_email=p.slack_id).one().body
    new = re.search(r"#t=([\w-]+)", body).group(1)
    assert client.post("/api/large/auth/invite/exchange", json={"token": new}).status_code == 200


def test_get_requests_never_change_state(client, db):
    w = make_world(db)
    login_participant(client, db, w.members[0][1])
    client.get("/api/large/me")
    client.get("/api/large/me/chat")
    assert db.query(Response).count() == 0
    db.rollback()  # 테스트 세션의 읽기 스냅샷을 끝낸다
    r = client.post("/api/large/me/response", json={"choice": "bus"})  # 명시적 제출만 반영
    assert r.status_code == 200 and db.query(Response).count() == 1


def test_state_change_requires_app_header(client, db):
    w = make_world(db)
    login_participant(client, db, w.members[0][1])
    r = client.post("/api/large/me/response", json={"choice": "bus"}, headers={"X-Requested-With": ""})
    assert r.status_code == 403 and r.json()["error"]["code"] == "CSRF_HEADER_MISSING"


def test_participant_cannot_reach_team_or_admin(client, db):
    w = make_world(db)
    login_participant(client, db, w.members[0][0])  # 담당자여도 참가자 세션만으로는 불가
    assert client.get("/api/large/team").status_code == 401
    assert client.get(f"/api/large/admin/events/{w.event.id}").status_code == 401


def _manager_login(client, db, p):
    login_id = p.employee_no
    assert client.post("/api/large/auth/manager/request-code", json={"login_id": login_id}).status_code == 200
    mail = db.query(DevMail).filter_by(to_email=p.slack_id).order_by(DevMail.id.desc()).first()
    code = re.search(r"인증코드: (\d{6})", mail.body).group(1)
    db.rollback()
    return client.post("/api/large/auth/manager/verify", json={"login_id": login_id, "code": code})


def test_manager_email_verification_and_team_scope(client, db):
    w = make_world(db)
    mgr = w.members[0][0]
    assert _manager_login(client, db, mgr).status_code == 200
    data = client.get("/api/large/team").json()
    assert data["team"]["id"] == w.teams[0].id
    assert {l["participant_id"] for l in data["billing"]["lines"]} == {p.id for p in w.members[0]}  # 자기 팀만


def test_manager_code_for_non_manager_is_generic_and_sends_nothing(client, db):
    w = make_world(db)
    member = w.members[0][1]
    r = client.post("/api/large/auth/manager/request-code", json={"login_id": member.employee_no})
    assert r.json()["sent"] is True  # 존재 여부를 알리지 않음
    assert db.query(DevMail).filter_by(to_email=member.slack_id).count() == 0
    assert client.post("/api/large/auth/manager/verify", json={"login_id": member.employee_no, "code": "000000"}).status_code == 401


def test_manager_code_attempt_limit(client, db):
    w = make_world(db)
    mgr = w.members[0][0]
    client.post("/api/large/auth/manager/request-code", json={"login_id": mgr.employee_no})
    codes = [client.post("/api/large/auth/manager/verify", json={"login_id": mgr.employee_no, "code": "999999"}).status_code for _ in range(6)]
    assert codes[:5] == [401] * 5 and codes[5] == 429


def test_manager_access_revoked_when_reassigned(client, db):
    w = make_world(db)
    assert _manager_login(client, db, w.members[0][0]).status_code == 200
    m = db.query(TeamManager).filter_by(team_id=w.teams[0].id).one()
    m.revoked_at = NOW
    db.add(TeamManager(team_id=w.teams[0].id, participant_id=w.members[0][1].id, assigned_by=w.admin.id))
    db.commit()
    assert client.get("/api/large/team").status_code == 403


def test_manager_must_be_team_member_and_only_admin_assigns(client, db):
    w = make_world(db)
    login_admin(client)
    url = f"/api/large/admin/events/{w.event.id}/teams/{w.teams[0].id}/manager"
    assert client.post(url, json={"participant_id": w.members[1][1].id}).json()["error"]["code"] == "NOT_TEAM_MEMBER"
    assert client.post(url, json={"participant_id": w.members[0][2].id}).status_code == 200


def test_admin_sees_only_own_event(client, db):
    mine = make_world(db)
    other = make_world(db, email="other@example.com", prefix="O")
    login_admin(client)
    assert client.get(f"/api/large/admin/events/{mine.event.id}").status_code == 200
    assert client.get(f"/api/large/admin/events/{other.event.id}").status_code == 404
    # 내 행사 경로에 남의 참가자 ID 를 넣어도 404
    r = client.post(f"/api/large/admin/events/{mine.event.id}/participants/{other.members[0][1].id}/invite/revoke")
    assert r.status_code == 404


def test_admin_login_wrong_password(client, db):
    make_world(db)
    r = client.post("/api/large/auth/admin/login", json={"email": "admin@example.com", "password": "nope"})
    assert r.status_code == 401


def test_logout_revokes_session(client, db):
    w = make_world(db)
    login_participant(client, db, w.members[0][1])
    assert client.get("/api/large/me").status_code == 200
    client.post("/api/large/auth/participant/logout")
    assert db.query(AuthSession).filter(AuthSession.revoked_at.is_not(None)).count() == 1


def test_demo_switch_is_absent_in_production(monkeypatch, db):
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import create_app

    w = make_world(db)
    monkeypatch.setattr(settings, "app_mode", "production")
    with TestClient(create_app()) as c:
        c.headers["X-Requested-With"] = "chongmu"
        assert c.post("/api/large/dev/switch", json={"role": "admin"}).status_code in (404, 405)
        assert c.get("/api/large/dev/inbox").status_code in (404, 405)
        assert c.get("/api/large/meta").json()["demo_tools"] is False


def test_production_requires_real_secrets():
    with pytest.raises(RuntimeError):
        Settings(app_mode="production").validate()
    Settings(app_mode="production", secret_key="s" * 40, admin_email="a@b.c", admin_password="x").validate()


def test_log_filter_redacts_tokens():
    rec = logging.LogRecord("x", logging.INFO, "", 0, 'POST /invite#t=abcDEF123456 {"token": "abcdefghijkl"}', None, None)
    RedactSecretsFilter().filter(rec)
    assert "abcDEF123456" not in rec.msg and "abcdefghijkl" not in rec.msg
