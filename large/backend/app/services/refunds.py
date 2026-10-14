"""환불 계좌 받기 → 총무 송금 처리.

- 환불 안내 DM(담당자에게만)에 '환불 받을 계좌 입력' 개인 링크(토큰, 7일)를 넣는다. 담당자는 그 링크나 담당자 화면에서 은행·계좌번호·예금주를 입력한다.
- 슬랙 답장("국민 123-45-678901 변수민")은 정산 Agent 가 읽어 계좌 '후보'로만 둔다(총무 확인 후 저장, HITL).
- 화면·보고서·CSV·감사 로그에는 뒤 4자리만. 전체 번호는 총무의 [전체 보기]·[계좌 복사]·송금 명세 CSV 에서만 꺼내고, 꺼낼 때마다 감사 로그에 남긴다(번호 없이).
- 실제 송금은 하지 않는다. 총무가 은행 앱에서 보낸 뒤 [송금 완료 표시] → 정산 확정 + 환불 지급 기록(Payout) → 잔액 0 → 완료.
- 정산이 끝난 부서는 [계좌 정보 삭제]로 지운다.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import clock
from ..config import settings
from ..errors import AppError, conflict, invalid, not_found
from ..models import Admin, EmailJob, Event, Participant, RefundAccount, RefundLink, Team
from ..security import hash_token, new_token
from . import settlement as stl
from .common import active_manager, audit, is_active_manager

LINK_TTL = timedelta(days=7)
BANKS = (
    "시연은행", "카카오뱅크", "토스뱅크", "케이뱅크", "새마을금고", "우체국", "국민", "KB", "신한", "우리", "하나", "농협", "NH", "기업", "IBK",
    "수협", "신협", "부산", "대구", "경남", "광주", "전북", "제주", "SC제일", "SC", "씨티", "산업", "카카오", "토스",
)
_BANK_RE = "|".join(sorted((re.escape(b) for b in BANKS), key=len, reverse=True))
# 은행 이름(목록에 있거나 '…은행') 바로 뒤의 숫자·하이픈 덩어리만 계좌로 본다(전화번호 같은 숫자는 은행 이름이 없으면 무시)
ACCOUNT_RE = re.compile(rf"(?P<bank>(?:{_BANK_RE})(?:은행)?|[가-힣A-Za-z]{{1,8}}은행)\s*[:：]?\s*(?P<no>\d[\d\- ]{{7,22}}\d)(?:\s*\(?\s*(?:예금주\s*[:：]?\s*)?(?P<holder>[가-힣]{{2,5}}|[A-Za-z][A-Za-z ]{{1,20}}[A-Za-z]))?")
HOLDER_RE = re.compile(r"^(?:[가-힣]{2,10}|[A-Za-z][A-Za-z .]{1,28}[A-Za-z])$")
BANK_FORM_RE = re.compile(r"^[가-힣A-Za-z0-9 ]{2,20}$")
DEMO_NOTE = "시연 중입니다. 실제 계좌는 넣지 마세요(예: 시연은행 000-0000-0001)."


def digits(no: str) -> str:
    return re.sub(r"\D", "", no or "")


def mask(no: str) -> str:
    d = digits(no)
    return f"····{d[-4:]}" if d else ""


def validate(bank: str, account_no: str, holder: str) -> tuple[str, str, str]:
    """형식 검사: 은행 2~20자, 계좌번호는 숫자·하이픈만(숫자 10~16자리), 예금주 2~10자(한글) 또는 영문."""
    bank, account_no, holder = (bank or "").strip(), (account_no or "").strip().replace(" ", ""), (holder or "").strip()
    errors = []
    if not BANK_FORM_RE.match(bank):
        errors.append({"field": "bank", "error": "은행 이름을 2~20자로 적어 주세요"})
    if not re.fullmatch(r"[\d-]+", account_no) or not 10 <= len(digits(account_no)) <= 16 or "--" in account_no or account_no.startswith("-") or account_no.endswith("-"):
        errors.append({"field": "account_no", "error": "계좌번호는 숫자와 하이픈(-)만, 숫자 10~16자리로 적어 주세요"})
    if not HOLDER_RE.match(holder):
        errors.append({"field": "holder", "error": "예금주는 2~10자 한글(또는 영문)로 적어 주세요"})
    if errors:
        raise invalid("ACCOUNT_INVALID", " · ".join(e["error"] for e in errors), errors)
    return bank, account_no, holder


def parse_account(text: str) -> dict[str, Any] | None:
    """슬랙 답장에서 '은행 계좌번호 (예금주)'를 찾는다. 형식 검사를 통과한 것만."""
    for m in ACCOUNT_RE.finditer(text or ""):
        no = m.group("no").strip().replace(" ", "-")
        if not 10 <= len(digits(no)) <= 16:
            continue
        return {"bank": m.group("bank"), "account_no": no, "holder": (m.group("holder") or "").strip(), "span": m.span("no")}
    return None


def masked_text(text: str, parsed: dict[str, Any]) -> str:
    """근거 메시지로 저장할 문장: 계좌번호 자리를 뒤 4자리만 남겨 가린다(전체 번호는 RefundAccount 에만)."""
    a, b = parsed["span"]
    return text[:a] + mask(parsed["account_no"]) + text[b:]


def account_out(a: RefundAccount | None, names: dict[int, str] | None = None) -> dict[str, Any] | None:
    if a is None:
        return None
    return {
        "id": a.id, "bank": a.bank, "masked": mask(a.account_no), "holder": a.holder, "status": a.status, "source": a.source,
        "source_label": {"manager": "담당자 화면 입력", "link": "개인 링크 입력", "slack": "슬랙 답장(정산 Agent 가 읽음)"}.get(a.source, a.source),
        "submitted_at": clock.fmt(a.submitted_at), "submitted_by": (names or {}).get(a.submitted_by) if a.submitted_by else None,
        "confirmed_at": clock.fmt(a.confirmed_at), "message_id": a.message_id,
    }


def saved(db: Session, team_id: int) -> RefundAccount | None:
    return db.scalar(select(RefundAccount).where(RefundAccount.team_id == team_id, RefundAccount.status == "saved").order_by(RefundAccount.id.desc()))


def candidate(db: Session, team_id: int) -> RefundAccount | None:
    return db.scalar(select(RefundAccount).where(RefundAccount.team_id == team_id, RefundAccount.status == "candidate").order_by(RefundAccount.id.desc()))


# ---------------------------------------------------------------- 개인 링크

def issue_link(db: Session, team: Team, participant: Participant, now: datetime | None = None) -> str:
    """환불 안내 DM 을 보낼 때 새로 만든다(원문은 DM 에만)."""
    now = now or clock.now()
    token = new_token()
    db.add(RefundLink(event_id=team.event_id, team_id=team.id, participant_id=participant.id, token_hash=hash_token(token), expires_at=now + LINK_TTL, created_at=now))
    db.flush()
    return token


def link_url(token: str) -> str:
    return f"{settings.public_base_url}/refund-account#t={token}"  # fragment(#) 라 서버 로그·Referer 로 새지 않음


def resolve_link(db: Session, token: str, now: datetime | None = None) -> RefundLink:
    now = now or clock.now()
    ln = db.scalar(select(RefundLink).where(RefundLink.token_hash == hash_token(token or "")))
    if ln is None:
        raise AppError(401, "LINK_INVALID", "유효하지 않은 링크입니다")
    if ln.expires_at <= now:
        raise AppError(401, "LINK_EXPIRED", "만료된 링크입니다(7일). 총무에게 다시 요청하세요")
    if not is_active_manager(db, ln.participant_id, ln.team_id):
        raise AppError(401, "LINK_INVALID", "담당자가 바뀌어 이 링크는 쓸 수 없습니다")
    return ln


# ---------------------------------------------------------------- 입력·후보·확인

def submit(db: Session, event: Event, team: Team, participant: Participant, bank: str, account_no: str, holder: str, *, source: str) -> RefundAccount:
    """담당자가 직접 입력(담당자 화면·개인 링크) → 그 부서의 '입력됨' 계좌. 이전 계좌·후보는 지운다."""
    if not is_active_manager(db, participant.id, team.id):
        raise AppError(403, "FORBIDDEN", "그 부서 담당자만 환불 계좌를 입력할 수 있습니다")
    bank, account_no, holder = validate(bank, account_no, holder)
    db.execute(delete(RefundAccount).where(RefundAccount.team_id == team.id))
    a = RefundAccount(event_id=event.id, team_id=team.id, bank=bank, account_no=account_no, holder=holder, status="saved", source=source, submitted_by=participant.id, submitted_at=clock.now())
    db.add(a)
    db.flush()
    audit(db, event.id, f"manager:{participant.id}", "refund_account_submitted", f"team:{team.id}", {"bank": bank, "account": mask(account_no), "source": source})
    return a


def add_candidate(db: Session, event: Event, team: Team, participant: Participant, parsed: dict[str, Any], message_id: int, at: datetime) -> RefundAccount | None:
    """슬랙 답장에서 읽은 계좌 → 후보(총무 확인 전). 형식이 틀리면 만들지 않는다."""
    try:
        bank, no, holder = validate(parsed["bank"], parsed["account_no"], parsed["holder"] or participant.name)
    except AppError:
        return None
    db.execute(delete(RefundAccount).where(RefundAccount.team_id == team.id, RefundAccount.status == "candidate"))
    a = RefundAccount(event_id=event.id, team_id=team.id, bank=bank, account_no=no, holder=holder, status="candidate", source="slack", submitted_by=participant.id, message_id=message_id, submitted_at=at)
    db.add(a)
    db.flush()
    return a


def drop_candidates(db: Session, message_ids: list[int]) -> None:
    if message_ids:
        db.execute(delete(RefundAccount).where(RefundAccount.status == "candidate", RefundAccount.message_id.in_(message_ids)))


def confirm_candidate(db: Session, event: Event, team: Team, account_id: int, admin: Admin) -> RefundAccount:
    a = db.get(RefundAccount, account_id)
    if a is None or a.team_id != team.id or a.status != "candidate":
        raise not_found("계좌 후보")
    db.execute(delete(RefundAccount).where(RefundAccount.team_id == team.id, RefundAccount.status == "saved"))
    a.status, a.confirmed_by, a.confirmed_at = "saved", admin.id, clock.now()
    audit(db, event.id, f"admin:{admin.id}", "refund_account_confirmed", f"team:{team.id}", {"bank": a.bank, "account": mask(a.account_no), "source": a.source})
    return a


def reveal(db: Session, event: Event, team: Team, admin: Admin, purpose: str) -> dict[str, Any]:
    """총무만: 전체 번호(보기·복사). 꺼낸 사실만 감사 로그에 남긴다(번호 없이)."""
    a = saved(db, team.id) or candidate(db, team.id)
    if a is None:
        raise not_found("환불 계좌")
    audit(db, event.id, f"admin:{admin.id}", "refund_account_revealed", f"team:{team.id}", {"purpose": purpose[:20], "account": mask(a.account_no)})
    return {"bank": a.bank, "account_no": a.account_no, "holder": a.holder, "status": a.status}


def remove(db: Session, event: Event, team: Team, admin: Admin) -> int:
    calc = stl.compute_team(db, event, team)
    if calc["balance"] != 0 or calc["blockers"]:
        raise conflict("NOT_SETTLED", "정산이 끝난(잔액 0원) 부서만 계좌 정보를 지울 수 있습니다")
    n = db.execute(delete(RefundAccount).where(RefundAccount.team_id == team.id)).rowcount or 0
    audit(db, event.id, f"admin:{admin.id}", "refund_account_deleted", f"team:{team.id}", {"rows": n})
    return n


# ---------------------------------------------------------------- 환불 송금 목록·송금 완료

STATE_LABEL = {"none": "미입력", "candidate": "후보(총무 확인 전)", "saved": "입력됨", "paid": "송금 완료"}


def rows(db: Session, event: Event) -> list[dict[str, Any]]:
    names = {p.id: p.name for p in db.scalars(select(Participant).where(Participant.event_id == event.id))}
    out = []
    for team in db.scalars(select(Team).where(Team.event_id == event.id).order_by(Team.code)):
        calc = stl.compute_team(db, event, team)
        acc, cand = saved(db, team.id), candidate(db, team.id)
        paid = calc["refunds_paid"]
        if calc["balance"] >= 0 and not paid and acc is None and cand is None:
            continue
        mgr = active_manager(db, team.id)
        notice = db.scalar(select(EmailJob).where(EmailJob.team_id == team.id, EmailJob.kind == "refund_notice", EmailJob.status == "sent").order_by(EmailJob.sent_at.desc()))
        link = db.scalar(select(RefundLink).where(RefundLink.team_id == team.id).order_by(RefundLink.id.desc()))
        due = max(0, -calc["balance"])
        state = "paid" if paid and due == 0 else "saved" if acc else "candidate" if cand else "none"
        out.append({
            "team_id": team.id, "team": team.name, "code": team.code, "manager": names.get(mgr.participant_id) if mgr else None,
            "refund": due + paid, "due": due, "paid": paid, "balance": calc["balance"], "blockers": calc["blockers"],
            "state": state, "state_label": STATE_LABEL[state], "account": account_out(acc, names), "candidate": account_out(cand, names),
            "notice_sent_at": clock.fmt(notice.sent_at) if notice else None, "link_expires_at": clock.fmt(link.expires_at) if link else None,
            "can_pay": due > 0 and acc is not None and not calc["blockers"], "can_delete": calc["balance"] == 0 and not calc["blockers"] and (acc is not None or cand is not None),
        })
    return out


def mark_paid(db: Session, event: Event, team: Team, admin: Admin) -> dict[str, Any]:
    """총무가 은행 앱에서 보낸 뒤 '송금 완료' 표시: 정산 확정(새 버전) + 남은 환불액만큼 지급 기록 → 잔액 0."""
    calc = stl.compute_team(db, event, team)
    due = max(0, -calc["balance"])
    if due == 0:
        raise conflict("NO_REFUND_DUE", "돌려줄 환불액이 없습니다")
    if saved(db, team.id) is None:
        raise conflict("ACCOUNT_MISSING", "환불 받을 계좌가 아직 없습니다(담당자 입력 또는 슬랙 후보 확인 필요)")
    stl.confirm(db, event, team.id, admin)
    p = stl.record_payout(db, event, team.id, due, "환불 송금(총무가 은행 앱에서 보냄)", admin)
    return {"team_id": team.id, "amount": p.amount, "payout_id": p.id}


def payout_sheet_csv(db: Session, event: Event, admin: Admin) -> str:
    """송금 명세 CSV — 전체 계좌번호가 들어가는 유일한 파일(내려받을 때 화면에서 한 번 더 확인)."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["부서", "담당자", "환불액", "남은 송금액", "은행", "계좌번호", "예금주", "계좌 상태", "송금 상태"])
    n = 0
    for r in rows(db, event):
        acc = saved(db, r["team_id"])
        w.writerow([r["team"], r["manager"] or "", r["refund"], r["due"], acc.bank if acc else "", acc.account_no if acc else "", acc.holder if acc else "",
                    "입력됨" if acc else STATE_LABEL["candidate" if r["candidate"] else "none"], "송금 완료" if r["state"] == "paid" else "송금 전"])
        n += 1
    audit(db, event.id, f"admin:{admin.id}", "payout_sheet_downloaded", "", {"rows": n})
    return buf.getvalue()


def team_state(db: Session, event: Event, team: Team) -> dict[str, Any] | None:
    """부서 상세의 '환불 송금' 칸(근거 패널에서 씀)."""
    return next((r for r in rows(db, event) if r["team_id"] == team.id), None)

