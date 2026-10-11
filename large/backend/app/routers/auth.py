"""로그인·초대 토큰 교환·담당자 이메일 인증. 모든 상태 변경은 POST 로만."""
from __future__ import annotations

import time
from collections import defaultdict, deque
from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import clock
from ..db import get_db
from ..deps import current_admin
from ..errors import AppError, unauthorized
from ..models import Admin, AuthSession, Event, ManagerCode, Participant
from ..schemas import AdminLogin, InviteExchange, ManagerCodeRequest, ManagerCodeVerify
from ..security import COOKIE_NAMES, create_session, hash_token, new_code, verify_password
from ..services import emails, invites
from ..services.common import active_manager, audit
from .util import clear_session_cookie, set_session_cookie

router = APIRouter(prefix="/api/auth", tags=["auth"])

_login_failures: dict[str, deque[float]] = defaultdict(deque)
LOGIN_WINDOW, LOGIN_MAX_FAILURES = 15 * 60, 5
CODE_TTL, CODE_MAX_ATTEMPTS, CODE_MAX_PER_HOUR = timedelta(minutes=10), 5, 5


def _revoke_cookie_session(db: Session, request: Request, role: str) -> None:
    token = request.cookies.get(COOKIE_NAMES[role])
    if token:
        s = db.scalar(select(AuthSession).where(AuthSession.token_hash == hash_token(token)))
        if s and s.revoked_at is None:
            s.revoked_at = clock.now()


@router.post("/admin/login")
def admin_login(body: AdminLogin, response: Response, db: Session = Depends(get_db)):
    key = body.email.strip().lower()
    fails = _login_failures[key]
    while fails and fails[0] < time.monotonic() - LOGIN_WINDOW:
        fails.popleft()
    if len(fails) >= LOGIN_MAX_FAILURES:
        raise AppError(429, "TOO_MANY_ATTEMPTS", "로그인 시도가 너무 많습니다. 15분 후 다시 시도하세요")
    admin = db.scalar(select(Admin).where(Admin.email == key))
    if admin is None or not verify_password(body.password, admin.password_hash):
        fails.append(time.monotonic())
        raise unauthorized("이메일 또는 비밀번호가 올바르지 않습니다")
    fails.clear()
    token = create_session(db, "admin", via="password", admin_id=admin.id)
    audit(db, None, f"admin:{admin.id}", "admin_login")
    db.commit()
    set_session_cookie(response, "admin", token)
    return {"admin": {"id": admin.id, "email": admin.email}}


@router.get("/admin/me")
def admin_me(admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    events = db.scalars(select(Event).where(Event.admin_id == admin.id).order_by(Event.id))
    return {"admin": {"id": admin.id, "email": admin.email}, "events": [{"id": e.id, "name": e.name} for e in events]}


@router.post("/{role}/logout")
def logout(role: str, request: Request, response: Response, db: Session = Depends(get_db)):
    if role not in COOKIE_NAMES:
        raise AppError(404, "NOT_FOUND", "알 수 없는 역할")
    _revoke_cookie_session(db, request, role)
    db.commit()
    clear_session_cookie(response, role)
    return {"ok": True}


@router.post("/invite/exchange")
def invite_exchange(body: InviteExchange, request: Request, response: Response, db: Session = Depends(get_db)):
    """초대 토큰(링크의 # 뒤 값)을 검증하고 참가자 세션 쿠키로 바꾼다. 프런트는 즉시 URL 에서 토큰을 지운다."""
    p = invites.resolve(db, body.token)
    _revoke_cookie_session(db, request, "participant")
    token = create_session(db, "participant", via="invite", event_id=p.event_id, participant_id=p.id, team_id=p.team_id)
    audit(db, p.event_id, f"participant:{p.id}", "invite_exchanged")
    db.commit()
    set_session_cookie(response, "participant", token)
    return {"participant": {"id": p.id, "name": p.name}}


def _managers_by_email(db: Session, email: str) -> list[tuple[Participant, int]]:
    out = []
    for p in db.scalars(select(Participant).where(Participant.email == email.strip().lower())):
        m = active_manager(db, p.team_id)
        if m is not None and m.participant_id == p.id:
            out.append((p, p.team_id))
    return out


GENERIC_SENT = {"sent": True, "message": "담당자로 등록된 이메일이면 인증코드를 보냈습니다. 메일함을 확인하세요."}


@router.post("/manager/request-code")
def manager_request_code(body: ManagerCodeRequest, db: Session = Depends(get_db)):
    """담당자 이메일 인증 1단계. 등록 여부와 관계없이 같은 응답을 준다(이메일 존재 여부 노출 방지)."""
    found = _managers_by_email(db, body.email)
    if not found:
        return GENERIC_SENT
    p, team_id = found[0]
    recent = db.scalar(
        select(func.count()).select_from(ManagerCode).where(ManagerCode.participant_id == p.id, ManagerCode.created_at >= clock.now() - timedelta(hours=1))
    )
    if recent >= CODE_MAX_PER_HOUR:
        return GENERIC_SENT
    code = new_code()
    mc = ManagerCode(participant_id=p.id, team_id=team_id, code_hash=hash_token(f"{p.id}:{code}"), expires_at=clock.now() + CODE_TTL)
    db.add(mc)
    db.flush()
    emails.send_manager_code(db, p, mc.id, code)
    db.commit()
    return GENERIC_SENT


@router.post("/manager/verify")
def manager_verify(body: ManagerCodeVerify, response: Response, db: Session = Depends(get_db)):
    found = _managers_by_email(db, body.email)
    if not found:
        raise unauthorized("인증코드가 올바르지 않습니다")
    p, team_id = found[0]
    mc = db.scalar(select(ManagerCode).where(ManagerCode.participant_id == p.id, ManagerCode.consumed_at.is_(None)).order_by(ManagerCode.id.desc()))
    if mc is None or mc.expires_at <= clock.now():
        raise unauthorized("인증코드가 만료되었습니다. 다시 요청하세요")
    if mc.attempts >= CODE_MAX_ATTEMPTS:
        raise AppError(429, "TOO_MANY_ATTEMPTS", "시도 횟수를 넘었습니다. 인증코드를 다시 요청하세요")
    mc.attempts += 1
    if mc.code_hash != hash_token(f"{p.id}:{body.code}"):
        db.commit()
        raise unauthorized("인증코드가 올바르지 않습니다")
    mc.consumed_at = clock.now()
    token = create_session(db, "manager", via="email_code", event_id=p.event_id, participant_id=p.id, team_id=team_id)
    audit(db, p.event_id, f"manager:{p.id}", "manager_verified", f"team:{team_id}")
    db.commit()
    set_session_cookie(response, "manager", token)
    return {"participant": {"id": p.id, "name": p.name}, "team_id": team_id}
