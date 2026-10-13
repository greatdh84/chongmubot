"""이메일 발송 스케줄러와 동시 실행 시 중복 발송 방지."""
import contextlib
import threading
import time
from datetime import timedelta

from app import clock, db as dbmod
from app.models import DevMail, EmailJob
from app.services import emails, responses
from app.services.scheduler import EmailScheduler

from .conftest import NOW, make_world


def _queue_invites(db, n_teams=4, size=5):
    w = make_world(db, n_teams=n_teams, size=size)
    emails.enqueue_invites(db, w.event)
    db.commit()
    return w


def test_claim_is_exclusive(db):
    _queue_invites(db, 1, 1)
    job = db.query(EmailJob).one()
    other = dbmod.SessionLocal()
    try:
        assert emails._claim(db, job.id, NOW, "A") is True
        assert emails._claim(other, job.id, NOW, "B") is False  # 다른 실행은 선점 실패
    finally:
        other.close()


def test_concurrent_manual_and_scheduler_send_each_mail_once(db, monkeypatch):
    w = _queue_invites(db)  # 20건
    # 같은 프로세스 잠금을 끄고 DB 선점만으로도 중복이 없는지 본다(=여러 프로세스 상황)
    monkeypatch.setattr(emails, "_dispatch_lock", contextlib.nullcontext())
    results = []

    def manual():
        s = dbmod.SessionLocal()
        try:
            results.append(emails.dispatch_due(s, s.get(type(w.event), w.event.id), now=NOW, source="manual"))
        finally:
            s.close()

    sched = EmailScheduler(interval=3600)
    threads = [threading.Thread(target=manual), threading.Thread(target=lambda: results.append(sched.run_once(now=NOW)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    db.rollback()  # 테스트 세션의 오래된 읽기 스냅샷을 끝냄
    per_job = {}
    for m in db.query(DevMail):
        per_job[m.email_job_id] = per_job.get(m.email_job_id, 0) + 1
    assert len(per_job) == 20 and set(per_job.values()) == {1}
    assert {j.status for j in db.query(EmailJob)} == {"sent"}


def test_stale_sending_job_is_reclaimed_with_note(db):
    w = _queue_invites(db, 1, 1)
    job = db.query(EmailJob).one()
    job.status, job.claimed_at = "sending", NOW - timedelta(minutes=11)
    db.commit()
    emails.dispatch_due(db, w.event, now=NOW)
    db.rollback()  # 테스트 세션의 오래된 읽기 스냅샷을 끝냄
    job = db.query(EmailJob).one()
    assert job.status == "failed" and "이미 발송됐을 수" in job.note  # 자동 재발송하지 않음
    assert db.query(DevMail).count() == 0


def test_scheduler_thread_runs_and_stops(db):
    w = _queue_invites(db, 1, 2)
    sched = EmailScheduler(interval=0.05)
    sched.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and db.query(DevMail).count() < 2:
            time.sleep(0.05)
            db.rollback()  # 테스트 세션의 오래된 읽기 스냅샷을 끝냄
    finally:
        sched.stop()
    assert db.query(DevMail).count() == 2
    assert not sched.running and sched.runs >= 1


def test_paused_scheduler_sends_nothing(db):
    _queue_invites(db, 1, 1)
    sched = EmailScheduler(interval=0.05)
    sched.paused = True
    sched.start()
    time.sleep(0.3)
    sched.stop()
    assert db.query(DevMail).count() == 0 and sched.runs == 0


def test_scheduler_respects_recheck_and_quiet_hours(db):
    w = make_world(db, n_teams=1, size=2)
    a, b = w.members[0]
    emails.enqueue_attend_reminders(db, w.event)
    responses.submit_change(db, w.event, a, "bus", source="web", actor="t")  # 발송 전에 응답 → 취소
    db.commit()
    sched = EmailScheduler(interval=3600)
    sched.run_once(now=NOW.replace(hour=14))  # UTC 14시 = KST 23시 → 야간 연기
    db.rollback()  # 테스트 세션의 오래된 읽기 스냅샷을 끝냄
    assert db.query(DevMail).count() == 0
    sched.run_once(now=NOW + timedelta(days=1))  # 다음날 낮(연기된 시각 이후)
    db.rollback()  # 테스트 세션의 오래된 읽기 스냅샷을 끝냄
    assert [m.to_email for m in db.query(DevMail)] == [b.email]
    assert db.query(EmailJob).filter_by(participant_id=a.id).one().note == "이미 응답함"


def test_toggle_api(client, db):
    w = make_world(db)
    client.post("/api/large/auth/admin/login", json={"email": "admin@example.com", "password": "pw-123456"})
    url = f"/api/large/admin/events/{w.event.id}/emails/scheduler"
    assert client.get(url).json()["running"] is False  # 테스트 설정에선 시작 안 함
    r = client.post(url, json={"enabled": True})
    assert r.status_code == 409 and r.json()["error"]["code"] == "SCHEDULER_NOT_STARTED"
    assert client.post(url, json={"enabled": False}).json()["paused"] is True
