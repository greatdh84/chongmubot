"""이메일: 대상 선정, 발송 직전 재검사, 금지 시간, 하루 한도, 중복 방지, 실패 재시도."""
from datetime import datetime, timedelta

from app import clock
from app.models import DevMail, EmailJob
from app.services import emails, payments, responses

from .conftest import NOW, make_world


def test_attend_reminder_only_unanswered_and_rechecked(db):
    w = make_world(db, n_teams=1, size=3)
    a, b, c = w.members[0]
    responses.submit_change(db, w.event, a, "bus", source="web", actor="t")
    responses.submit_change(db, w.event, b, "undecided", source="web", actor="t")  # 미정도 독촉 대상
    assert emails.enqueue_attend_reminders(db, w.event)["created"] == 2
    assert emails.enqueue_attend_reminders(db, w.event)["created"] == 0  # 같은 날 중복 생성 안 함
    responses.submit_change(db, w.event, c, "car", source="web", actor="t")  # 발송 전에 응답함
    result = emails.dispatch_due(db, w.event)
    db.commit()
    assert result.get("sent") == 1 and result.get("skipped") == 1
    assert db.query(EmailJob).filter_by(participant_id=c.id).one().note == "이미 응답함"
    assert [m.to_email for m in db.query(DevMail)] == [b.slack_id]


def test_quiet_hours_defer(db):
    w = make_world(db, n_teams=1, size=1)
    emails.enqueue_invites(db, w.event)
    night = datetime(2026, 10, 8, 14, 0)  # UTC 14:00 = KST 23:00
    out = emails.dispatch_due(db, w.event, now=night)
    job = db.query(EmailJob).one()
    assert out.get("deferred") == 1 and job.status == "queued"
    assert clock.kst(job.scheduled_at).hour == 8
    assert db.query(DevMail).count() == 0


def test_daily_limit_defers(db, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "daily_limit", 1)
    w = make_world(db, n_teams=1, size=1)
    p = w.members[0][0]
    emails.enqueue_invites(db, w.event)
    emails.dispatch_due(db, w.event)
    emails.enqueue_attend_reminders(db, w.event)
    out = emails.dispatch_due(db, w.event)
    assert out.get("deferred") == 1
    assert db.query(EmailJob).filter_by(kind="attend_reminder").one().note.startswith("하루 최대")
    assert db.query(DevMail).filter_by(to_email=p.slack_id).count() == 1


def test_failure_retry_then_failed(db):
    w = make_world(db, n_teams=1, size=1)
    p = w.members[0][0]
    p.slack_id = "UBOUNCE001"
    emails.enqueue_invites(db, w.event)
    now = NOW
    statuses = []
    for _ in range(3):
        emails.dispatch_due(db, w.event, now=now)
        job = db.query(EmailJob).one()
        statuses.append(job.status)
        now = job.scheduled_at + timedelta(seconds=1)
    assert statuses == ["retry_wait", "retry_wait", "failed"] and job.attempts == 3 and job.last_error


def test_payment_reminder_targets_unpaid_team_managers_without_amounts(db):
    w = make_world(db, n_teams=2, size=2)
    for ps in w.members.values():
        for p in ps:
            responses.submit_change(db, w.event, p, "bus", source="web", actor="t")
    payments.import_transactions(db, w.event, "거래일시,입금자명,금액,메모\n2026-10-07 10:00,x,40000,T01\n".encode(), "t.csv", "t")
    txn = db.query(payments.BankTransaction).one()
    payments.allocate(db, w.event, txn.id, w.teams[0].id, 40000, status="confirmed", source="admin", basis="", actor="t")
    assert emails.enqueue_payment_reminders(db, w.event)["created"] == 1
    emails.dispatch_due(db, w.event)
    mail = db.query(DevMail).one()
    assert mail.to_email == w.members[1][0].slack_id
    assert "40,000" not in mail.body and "/manager/login" in mail.body  # 금액 대신 확인 링크


def test_payment_reminder_skipped_if_paid_before_send(db):
    w = make_world(db, n_teams=1, size=1)
    responses.submit_change(db, w.event, w.members[0][0], "bus", source="web", actor="t")
    emails.enqueue_payment_reminders(db, w.event)
    payments.import_transactions(db, w.event, "거래일시,입금자명,금액,메모\n2026-10-07 10:00,x,20000,T01\n".encode(), "t.csv", "t")
    txn = db.query(payments.BankTransaction).one()
    payments.allocate(db, w.event, txn.id, w.teams[0].id, 20000, status="confirmed", source="admin", basis="", actor="t")
    emails.dispatch_due(db, w.event)
    assert db.query(EmailJob).one().status == "skipped" and db.query(DevMail).count() == 0


def test_invite_mail_token_not_stored_in_job(db):
    w = make_world(db, n_teams=1, size=1)
    emails.enqueue_invites(db, w.event)
    emails.dispatch_due(db, w.event)
    db.commit()
    job = db.query(EmailJob).one()
    body = db.query(DevMail).one().body
    token = body.split("#t=")[1].split()[0]
    assert token not in (job.subject + job.note + job.last_error + job.dedupe_key)


def test_payment_reminder_waits_only_for_deposit_confirmations(db):
    from app.services.common import create_task

    w = make_world(db, n_teams=1, size=1)
    responses.submit_change(db, w.event, w.members[0][0], "bus", source="web", actor="t")
    base = dict(event_id=w.event.id, assignee="manager", team_id=w.teams[0].id, question="q", created_by="t")
    create_task(db, dedupe_key="u", kind="unresolved_response", **base)  # 입금과 무관 → 독촉 막지 않음
    emails.enqueue_payment_reminders(db, w.event)
    emails.dispatch_due(db, w.event)
    assert db.query(EmailJob).one().status == "sent"
    create_task(db, dedupe_key="d", kind="unknown_depositor", **base)  # 입금 확인 중 → 독촉 보류
    clock.freeze(NOW + timedelta(days=1))
    emails.enqueue_payment_reminders(db, w.event)
    emails.dispatch_due(db, w.event)
    assert db.query(EmailJob).order_by(EmailJob.id.desc()).first().note == "입금 확인 대기 중인 과제가 있음"
