"""알림 발송 어댑터(Slack DM). 파일 이름은 이전 버전 호환을 위해 그대로 둔다.

NOTIFY_MODE=mock  : 실제로 보내지 않고 개발용 수신함(dev_inbox)에 기록한다.
NOTIFY_MODE=slack : 소모임과 같은 Slack 봇 토큰(SLACK_BOT_TOKEN)으로 받는 사람에게 DM 을 보낸다.
                    conversations.open → chat.postMessage. 필요한 권한: im:write, chat:write (소모임 봇에 이미 있음)
"""
from __future__ import annotations

from typing import Protocol

import httpx
from sqlalchemy.orm import Session

from ..config import settings
from ..models import DevMail


class EmailSendError(Exception):
    """발송 실패. 이름은 이전 버전 호환용(실제로는 Slack DM 실패)."""


class EmailAdapter(Protocol):
    mode: str

    def send(self, db: Session, *, to: str, subject: str, body: str, job_id: int | None) -> None: ...


class MockEmailAdapter:
    mode = "mock"

    def send(self, db: Session, *, to: str, subject: str, body: str, job_id: int | None) -> None:
        # 시연용 실패 재현: Slack ID 에 'BOUNCE' 가 들어 있으면 발송 실패로 처리
        if "bounce" in to.lower():
            raise EmailSendError("모의 실패: Slack 에서 사용자를 찾을 수 없습니다(user_not_found)")
        db.add(DevMail(email_job_id=job_id, to_email=to, subject=subject, body=body))


class SlackDmAdapter:
    mode = "slack"
    api = "https://slack.com/api/"

    def _call(self, method: str, payload: dict) -> dict:
        try:
            r = httpx.post(self.api + method, headers={"Authorization": f"Bearer {settings.slack_bot_token}"}, json=payload, timeout=15)
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise EmailSendError(f"Slack 연결 오류: {type(e).__name__}") from None
        if not data.get("ok"):
            # 오류 코드만 남긴다(본문·링크·토큰이 로그에 섞이지 않게)
            raise EmailSendError(f"Slack 오류: {str(data.get('error', 'unknown'))[:80]}")
        return data

    def send(self, db: Session, *, to: str, subject: str, body: str, job_id: int | None) -> None:
        channel = self._call("conversations.open", {"users": to})["channel"]["id"]
        self._call("chat.postMessage", {"channel": channel, "text": f"*{subject}*\n{body}", "unfurl_links": False})


def get_email_adapter() -> EmailAdapter:
    return SlackDmAdapter() if settings.email_mode == "slack" else MockEmailAdapter()
