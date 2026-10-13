"""이메일 발송 작업: 생성(중복 방지 키) → 발송 직전 재검사 → 발송/연기/건너뜀/재시도.

- 하루 최대 횟수: 수신자별 settings.daily_limit (인증코드 메일 제외). 넘으면 다음날 발송 가능 시각으로 연기.
- 전송 금지 시간: settings.quiet_start_hour ~ quiet_end_hour(KST). 그 사이면 허용 시각으로 연기.
- 실패: attempts 증가, max_attempts 미만이면 retry_wait(지수 백오프), 넘으면 failed.
- 본문에 들어가는 초대 토큰은 발송 시점에 새로 발급하며 작업 테이블에는 저장하지 않는다.
"""
from __future__ import annotations

import os
import socket
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import clock
from ..adapters.email import EmailSendError, get_email_adapter
from ..config import settings
from ..models import ConfirmTask, EmailJob, Event, Participant, Response, Team
from . import invites
from .common import active_manager, audit

QUIET_EXEMPT = {"manager_code"}  # 사용자가 직접 요청한 인증코드는 즉시 발송
# 이 과제가 열려 있으면 '이미 낸 돈이 아직 확인 중'일 수 있어 입금 독촉을 보내지 않는다.
DEPOSIT_TASK_KINDS = ("unknown_depositor", "ambiguous_depositor", "unclear_deposit", "possible_duplicate")


def _enqueue(db: Session, *, dedupe_key: str, **fields) -> tuple[EmailJob, bool]:
    existing = db.scalar(select(EmailJob).where(EmailJob.dedupe_key == dedupe_key))
    if existing is not None:
        return existing, False
    job = EmailJob(dedupe_key=dedupe_key, scheduled_at=clock.now(), **fields)
    db.add(job)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        return db.scalar(select(EmailJob).where(EmailJob.dedupe_key == dedupe_key)), False
    return job, True


def enqueue_invites(db: Session, event: Event, participant_ids: list[int] | None = None) -> dict:
    q = select(Participant).where(Participant.event_id == event.id)
    if participant_ids:
        q = q.where(Participant.id.in_(participant_ids))
    created = 0
    for p in db.scalars(q):
        _, new = _enqueue(db, dedupe_key=f"invite:{p.id}:initial", event_id=event.id, kind="invite", participant_id=p.id, team_id=p.team_id, to_email=p.email)
        created += new
    return {"created": created}


def enqueue_reissue(db: Session, event: Event, participant: Participant) -> EmailJob:
    job, _ = _enqueue(
        db,
        dedupe_key=f"invite:{participant.id}:reissue:{clock.now().isoformat()}",
        event_id=event.id,
        kind="invite",
        participant_id=participant.id,
        team_id=participant.team_id,
        to_email=participant.email,
    )
    return job


def _unanswered(db: Session, participant_id: int) -> bool:
    r = db.scalar(select(Response).where(Response.participant_id == participant_id))
    return r is None or r.choice == "undecided"


def enqueue_attend_reminders(db: Session, event: Event) -> dict:
    """미응답(미정 포함) 개인에게만."""
    day = clock.kst_date_key(clock.now())
    created = 0
    for p in db.scalars(select(Participant).where(Participant.event_id == event.id)):
        if _unanswered(db, p.id):
            _, new = _enqueue(db, dedupe_key=f"attend:{p.id}:{day}", event_id=event.id, kind="attend_reminder", participant_id=p.id, team_id=p.team_id, to_email=p.email)
            created += new
    return {"created": created}


def enqueue_payment_reminders(db: Session, event: Event) -> dict:
    """정산 잔액이 남은(미납) 팀의 담당자에게만."""
    from .settlement import compute_team

    day = clock.kst_date_key(clock.now())
    created = no_manager = 0
    for team in db.scalars(select(Team).where(Team.event_id == event.id)):
        if compute_team(db, event, team)["balance"] <= 0:
            continue
        mgr = active_manager(db, team.id)
        if mgr is None:
            no_manager += 1
            continue
        p = db.get(Participant, mgr.participant_id)
        _, new = _enqueue(db, dedupe_key=f"payment:{team.id}:{day}", event_id=event.id, kind="payment_reminder", participant_id=p.id, team_id=team.id, to_email=p.email)
        created += new
    return {"created": created, "teams_without_manager": no_manager}


def enqueue_confirm_request(db: Session, task: ConfirmTask) -> EmailJob | None:
    if task.assignee != "manager" or task.team_id is None:
        return None
    mgr = active_manager(db, task.team_id)
    if mgr is None:
        return None
    p = db.get(Participant, mgr.participant_id)
    job, _ = _enqueue(db, dedupe_key=f"confirm:{task.id}", event_id=task.event_id, kind="confirm_request", participant_id=p.id, team_id=task.team_id, task_id=task.id, to_email=p.email)
    return job


def send_manager_code(db: Session, participant: Participant, code_id: int, code: str) -> EmailJob:
    job, _ = _enqueue(db, dedupe_key=f"manager_code:{code_id}", event_id=participant.event_id, kind="manager_code", participant_id=participant.id, team_id=participant.team_id, to_email=participant.email)
    _dispatch_one(db, job, clock.now(), code=code)
    return job


# ---------------------------------------------------------------- 발송

def _recheck(db: Session, job: EmailJob) -> str | None:
    """발송 직전 재검사. 보낼 필요가 없어졌으면 건너뛸 사유를 돌려준다."""
    event = db.get(Event, job.event_id)
    if job.kind == "attend_reminder":
        if not _unanswered(db, job.participant_id):
            return "이미 응답함"
    elif job.kind == "payment_reminder":
        from .settlement import compute_team

        team = db.get(Team, job.team_id)
        mgr = active_manager(db, team.id)
        if mgr is None or mgr.participant_id != job.participant_id:
            return "담당자가 바뀜"
        if compute_team(db, event, team)["balance"] <= 0:
            return "잔액 없음(납부 완료)"
        waiting = db.scalar(
            select(func.count()).select_from(ConfirmTask).where(
                ConfirmTask.team_id == team.id, ConfirmTask.kind.in_(DEPOSIT_TASK_KINDS), ConfirmTask.status.in_(("open", "answered"))
            )
        )
        if waiting:
            return "입금 확인 대기 중인 과제가 있음"
    elif job.kind == "confirm_request":
        task = db.get(ConfirmTask, job.task_id)
        if task is None or task.status != "open":
            return "확인 과제가 이미 처리됨"
        mgr = active_manager(db, task.team_id)
        if mgr is None or mgr.participant_id != job.participant_id:
            return "담당자가 바뀜"
    return None


def _render(db: Session, job: EmailJob, code: str | None = None) -> tuple[str, str]:
    event = db.get(Event, job.event_id)
    p = db.get(Participant, job.participant_id)
    deadline = clock.kst(event.attend_deadline).strftime("%m월 %d일 %H:%M")
    pay_deadline = clock.kst(event.payment_deadline).strftime("%m월 %d일 %H:%M")
    manager_url = f"{settings.public_base_url}/manager/login"
    if job.kind in ("invite", "attend_reminder"):
        link = invites.link(invites.issue(db, p))  # 발송 시점에 새 토큰 발급(원문은 메일에만 존재)
        head = "참석 여부를 알려주세요" if job.kind == "invite" else "아직 참석 응답이 없어요"
        body = (
            f"{p.name}님, 안녕하세요.\n\n[{event.name}] {head}.\n"
            f"아래 개인 링크에서 참석+버스 / 참석+자차 / 불참 / 미정 중 하나를 골라 '제출'을 눌러 주세요.\n\n"
            f"{link}\n\n"
            f"- 참석 응답 마감: {deadline}\n- 이 링크는 본인 전용입니다. 다른 사람과 공유하지 마세요.\n"
            f"- 마감 후 변경은 총무 승인과 환불 규칙에 따라 처리됩니다."
        )
        return f"[{event.name}] {head}", body
    if job.kind == "payment_reminder":
        team = db.get(Team, job.team_id)
        body = (
            f"{p.name}님, [{event.name}] {team.name} 담당자로 안내드립니다.\n\n"
            f"팀 회비 정산에 아직 납부할 금액이 남아 있습니다. 팀 입금 마감: {pay_deadline}\n"
            f"금액과 명세는 메일에 적지 않습니다. 아래 담당자 화면에서 이메일 인증 후 확인해 주세요.\n\n{manager_url}"
        )
        return f"[{event.name}] {team.name} 회비 입금 안내", body
    if job.kind == "confirm_request":
        body = (
            f"{p.name}님, [{event.name}] 정산 Agent 가 확인을 요청했습니다.\n\n"
            f"질문 내용은 담당자 화면의 '확인 요청'에서 볼 수 있습니다. 답변해 주시면 정산안에 반영됩니다.\n\n{manager_url}"
        )
        return f"[{event.name}] 회비 확인 요청이 도착했습니다", body
    if job.kind == "manager_code":
        body = f"담당자 화면 인증코드: {code}\n10분 동안 유효합니다. 본인이 요청하지 않았다면 이 메일을 무시하세요."
        return f"[{event.name}] 담당자 인증코드", body
    raise ValueError(job.kind)


def _sent_today(db: Session, to_email: str, now: datetime) -> int:
    local = clock.kst(now)
    start = clock.to_utc_naive(local.replace(hour=0, minute=0, second=0, microsecond=0))
    return db.scalar(
        select(func.count()).select_from(EmailJob).where(
            EmailJob.to_email == to_email, EmailJob.status == "sent", EmailJob.kind.not_in(QUIET_EXEMPT), EmailJob.sent_at >= start
        )
    )


def _dispatch_one(db: Session, job: EmailJob, now: datetime, code: str | None = None) -> str:
    if job.kind not in QUIET_EXEMPT:
        later = clock.next_allowed_send_time(now, settings.quiet_start_hour, settings.quiet_end_hour)
        if later is not None:
            job.status, job.scheduled_at, job.note = "queued", later, "전송 금지 시간 → 연기"
            return "deferred"
        if _sent_today(db, job.to_email, now) >= settings.daily_limit:
            tomorrow = clock.kst(now).replace(hour=settings.quiet_end_hour, minute=0, second=0, microsecond=0) + timedelta(days=1)
            job.status, job.scheduled_at, job.note = "queued", clock.to_utc_naive(tomorrow), "하루 최대 발송 횟수 초과 → 다음날로 연기"
            return "deferred"
    reason = _recheck(db, job)
    if reason:
        job.status, job.note = "skipped", reason
        return "skipped"
    try:
        subject, body = _render(db, job, code)
        job.subject = subject
        get_email_adapter().send(db, to=job.to_email, subject=subject, body=body, job_id=job.id)
    except EmailSendError as e:
        job.attempts += 1
        job.last_error = str(e)[:300]
        if job.attempts >= job.max_attempts:
            job.status = "failed"
        else:
            job.status = "retry_wait"
            job.scheduled_at = now + timedelta(minutes=5 * 2 ** (job.attempts - 1))
        return job.status
    job.attempts += 1
    job.status, job.sent_at, job.last_error, job.note = "sent", now, "", ""
    return "sent"


def _process_claimed(db: Session, job_id: int, now: datetime) -> str:
    """선점한 작업 처리. 다른 연결이 동시에 쓰면 SQLite 가 'locked' 를 낼 수 있어 짧게 재시도한다(선점은 유지)."""
    for attempt in range(5):
        try:
            job = db.get(EmailJob, job_id)
            db.refresh(job)
            outcome = _dispatch_one(db, job, now)
            db.commit()
            return outcome
        except OperationalError as e:
            db.rollback()
            if "locked" not in str(e.orig) or attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))
    raise RuntimeError("unreachable")


# 같은 프로세스 안에서 수동 실행('지금 발송 실행')과 스케줄러가 겹치지 않게 하는 잠금.
# 다른 프로세스와의 겹침은 아래 DB 선점(_claim)이 막는다.
_dispatch_lock = threading.Lock()
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
STALE_CLAIM = timedelta(minutes=10)


def _claim(db: Session, job_id: int, now: datetime, worker: str) -> bool:
    """queued/retry_wait → sending 을 한 문장으로 바꾼다. 바뀐 행이 1개일 때만 이 실행이 보낼 권리를 가진다."""
    res = db.execute(
        update(EmailJob)
        .where(EmailJob.id == job_id, EmailJob.status.in_(("queued", "retry_wait")), EmailJob.scheduled_at <= now)
        .values(status="sending", claimed_at=now, claimed_by=worker[:100])
    )
    db.commit()
    return res.rowcount == 1


def _reclaim_stale(db: Session, event: Event, now: datetime) -> int:
    """발송 도중 프로세스가 죽어 sending 으로 남은 작업은 이미 나갔을 수도 있다.
    중복 발송을 피하려고 자동 재발송하지 않고 failed + 메모로 남겨 총무가 확인 후 다시 보내게 한다."""
    res = db.execute(
        update(EmailJob)
        .where(EmailJob.event_id == event.id, EmailJob.status == "sending", EmailJob.claimed_at < now - STALE_CLAIM)
        .values(status="failed", note="발송 중 중단됨 — 이미 발송됐을 수 있음. 확인 후 필요하면 다시 보내세요")
    )
    db.commit()
    return res.rowcount or 0


def dispatch_due(db: Session, event: Event, now: datetime | None = None, limit: int = 500, source: str = "manual") -> dict:
    """발송할 때가 된 작업을 하나씩 선점해 처리한다. 작업마다 커밋하므로 중간에 멈춰도 이미 보낸 기록은 남는다."""
    with _dispatch_lock:
        now = now or clock.now()
        worker = f"{WORKER_ID}:{source}"
        reclaimed = _reclaim_stale(db, event, now)
        ids = list(
            db.scalars(
                select(EmailJob.id)
                .where(EmailJob.event_id == event.id, EmailJob.status.in_(("queued", "retry_wait")), EmailJob.scheduled_at <= now)
                .order_by(EmailJob.id)
                .limit(limit)
            )
        )
        db.commit()
        counts: dict[str, int] = {}
        for jid in ids:
            if not _claim(db, jid, now, worker):
                counts["claimed_elsewhere"] = counts.get("claimed_elsewhere", 0) + 1
                continue
            try:
                outcome = _process_claimed(db, jid, now)
            except Exception as e:  # 렌더링 오류 등 예상 밖 실패: 이 작업만 재시도 대기로
                db.rollback()
                job = db.get(EmailJob, jid)
                job.attempts += 1
                job.status = "failed" if job.attempts >= job.max_attempts else "retry_wait"
                job.scheduled_at = now + timedelta(minutes=5)
                job.last_error = f"처리 오류: {type(e).__name__}"[:300]
                db.commit()
                outcome = "error"
            counts[outcome] = counts.get(outcome, 0) + 1
        if ids or reclaimed:
            audit(db, event.id, f"system:{source}", "emails_dispatched", detail=counts | ({"reclaimed": reclaimed} if reclaimed else {}))
            db.commit()
        return {"processed": len(ids), **counts, "email_mode": settings.email_mode, "source": source}
