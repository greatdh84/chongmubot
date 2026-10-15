"""Slack 실시간 연동: Slack Events API → POST /api/large/slack/events → ingest_slack_message.

슬랙 대화 CSV(시연)는 이 경로를 '재생'하는 것이고, 실제 운영에서는 Slack 이 메시지마다 여기로 이벤트를 보낸다.
- 기본 꺼짐(SLACK_EVENTS=on 으로 켬). 꺼져 있으면 404.
- 서명 검증 필수: SLACK_SIGNING_SECRET 으로 v0=HMAC-SHA256("v0:{timestamp}:{body}") 비교, 5분 넘은 요청 거부. 비밀값이 없거나 틀리면 403.
- url_verification → challenge 응답. event_callback 의 message(DM message.im · 채널 메시지)만 처리, 봇·수정·삭제(subtype) 무시.
- 보낸 사람 Slack ID 로 명단을 찾는다(대모임 행사 참가자). 명단에 없으면 기록하지 않고 200(Slack 재전송 방지).
- 같은 event_id 는 한 번만 처리(Slack 재전송).
쿠키 세션을 쓰지 않으므로 CSRF 헤더 검사에서 이 경로만 빠진다(main.py).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..config import API_PREFIX, settings
from ..db import get_db
from ..errors import forbidden, not_found
from ..models import Event, EvidenceMessage, Participant
from ..services import report, slack

router = APIRouter(prefix=API_PREFIX + "/slack", tags=["slack"])
MAX_AGE = 60 * 5
MENTION = re.compile(r"([가-힣]{2,4})\s*님")


def verify_signature(secret: str, timestamp: str, body: bytes, signature: str, now: float) -> bool:
    if not secret or not timestamp or not signature:
        return False
    try:
        if abs(now - int(timestamp)) > MAX_AGE:
            return False
    except ValueError:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _related(db: Session, sender: Participant, text: str) -> Participant:
    """메시지가 말하는 사람: 같은 부서 팀원 이름이 'OO님'으로 정확히 한 명 나오면 그 사람, 아니면 보낸 사람 본인."""
    names = {m.group(1) for m in MENTION.finditer(text)} - {sender.name}
    if names:
        hits = list(db.scalars(select(Participant).where(Participant.team_id == sender.team_id, Participant.name.in_(names))))
        if len(hits) == 1:
            return hits[0]
    return sender


@router.post("/events")
async def slack_events(request: Request, db: Session = Depends(get_db)):
    if not settings.slack_events:
        raise not_found("API")
    body = await request.body()
    ok = verify_signature(
        settings.slack_signing_secret, request.headers.get("x-slack-request-timestamp", ""), body,
        request.headers.get("x-slack-signature", ""), clock.now().replace(tzinfo=timezone.utc).timestamp(),
    )
    if not ok:
        raise forbidden("Slack 서명이 맞지 않습니다")
    try:
        payload = json.loads(body)
    except ValueError:
        return JSONResponse({"ok": False, "ignored": "not json"})
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}
    if payload.get("type") != "event_callback":
        return {"ok": True, "ignored": "type"}
    ev = payload.get("event") or {}
    if ev.get("type") != "message" or ev.get("subtype") or ev.get("bot_id") or not ev.get("user") or not str(ev.get("text", "")).strip():
        return {"ok": True, "ignored": "not a user message"}
    event_id = str(payload.get("event_id") or ev.get("client_msg_id") or "")
    if event_id and db.scalar(select(EvidenceMessage.id).where(EvidenceMessage.external_id == event_id)):
        return {"ok": True, "duplicate": True}
    sender = db.scalar(
        select(Participant).join(Event, Event.id == Participant.event_id).where(Participant.slack_id == ev["user"], Event.mode == "large").order_by(Participant.id.desc())
    )
    if sender is None:
        return {"ok": True, "ignored": "sender not in roster"}
    event = db.get(Event, sender.event_id)
    try:
        sent_at = datetime.fromtimestamp(float(ev.get("ts", "0")), tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        sent_at = clock.now()
    text = str(ev["text"])[:2000]
    m = slack.ingest_slack_message(
        db, event, sent_at=sent_at, sender=sender.name, sender_slack_id=sender.slack_id, text=text,
        channel=str(ev.get("channel", "")), recipient="총무봇" if ev.get("channel_type") == "im" else f"#{ev.get('channel', '')}",
        related=_related(db, sender, text), source="slack_event", external_id=event_id,
    )
    report.refresh(db, event, "slack_event")
    db.commit()
    a = m.analysis or {}
    return {"ok": True, "message_id": m.id, "kind": a.get("kind"), "outcome": a.get("outcome"), "change_id": a.get("change_id")}
