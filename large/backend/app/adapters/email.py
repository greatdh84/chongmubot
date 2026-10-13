"""이메일 어댑터. EMAIL_MODE=mock 이면 실제로 보내지 않고 개발용 수신함(dev_inbox)에 기록한다."""
from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Protocol

from sqlalchemy.orm import Session

from ..config import settings
from ..models import DevMail


class EmailSendError(Exception):
    pass


class EmailAdapter(Protocol):
    mode: str

    def send(self, db: Session, *, to: str, subject: str, body: str, job_id: int | None) -> None: ...


class MockEmailAdapter:
    mode = "mock"

    def send(self, db: Session, *, to: str, subject: str, body: str, job_id: int | None) -> None:
        # 시연용 실패 재현: 주소에 'bounce' 가 들어 있으면 발송 실패로 처리
        if "bounce" in to:
            raise EmailSendError("모의 반송: 수신 서버가 메일을 거부했습니다")
        db.add(DevMail(email_job_id=job_id, to_email=to, subject=subject, body=body))


class SmtpEmailAdapter:
    mode = "smtp"

    def send(self, db: Session, *, to: str, subject: str, body: str, job_id: int | None) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = settings.smtp_from, to, subject
        msg.set_content(body)
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
                smtp.starttls()
                if settings.smtp_user:
                    smtp.login(settings.smtp_user, settings.smtp_password)
                smtp.send_message(msg)
        except (smtplib.SMTPException, OSError) as e:
            # 예외 메시지에 본문(링크)이 섞이지 않도록 종류만 남긴다
            raise EmailSendError(f"SMTP 오류: {type(e).__name__}") from None


def get_email_adapter() -> EmailAdapter:
    return SmtpEmailAdapter() if settings.email_mode == "smtp" else MockEmailAdapter()
