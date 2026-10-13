"""거래내역 CSV 업로드와 입금 배정.

- 같은 거래 행은 row_hash 로 중복 저장하지 않는다(같은 파일을 두 번 올려도 안전).
- 배정 합계(취소 제외)는 거래 금액을 넘을 수 없다. 같은 거래·팀·개인 조합의 활성 배정은 하나만.
- 정산에는 confirmed 배정만 들어간다.
"""
from __future__ import annotations

import csv
import hashlib
import io
from collections import Counter
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import clock
from ..errors import conflict, invalid, not_found
from ..models import Allocation, BankTransaction, Event, Participant, Upload
from .common import audit, get_participant_in_event, get_team_in_event
from .roster import _decode

TXN_HEADERS = {
    "거래일시": "txn_at", "txn_at": "txn_at", "date": "txn_at",
    "입금자명": "depositor", "입금자": "depositor", "depositor": "depositor",
    "금액": "amount", "입금액": "amount", "amount": "amount",
    "메모": "memo", "적요": "memo", "memo": "memo",
}


def _parse_amount(text: str) -> int:
    cleaned = text.replace(",", "").replace("원", "").strip()
    if not cleaned.lstrip("-").isdigit():
        raise ValueError(f"금액 형식 오류: {text!r}")
    return int(cleaned)


def import_transactions(db: Session, event: Event, data: bytes, filename: str, actor: str) -> dict:
    reader = csv.DictReader(io.StringIO(_decode(data)))
    cols = {TXN_HEADERS.get((h or "").strip()) for h in reader.fieldnames or []}
    if not {"txn_at", "depositor", "amount"} <= cols:
        raise invalid("CSV_COLUMNS", "거래내역 CSV 에는 거래일시, 입금자명, 금액 열이 필요합니다(메모 선택)")
    parsed, errors, skipped_nonpositive = [], [], 0
    occurrences: Counter[str] = Counter()
    for i, raw in enumerate(reader, start=2):
        row = {TXN_HEADERS[k.strip()]: (v or "").strip() for k, v in raw.items() if k and k.strip() in TXN_HEADERS}
        try:
            amount = _parse_amount(row["amount"])
            txn_at = clock.parse_input(row["txn_at"].replace(" ", "T", 1) if "T" not in row["txn_at"] else row["txn_at"])
        except (ValueError, KeyError) as e:
            errors.append({"line": i, "error": str(e)})
            continue
        if amount <= 0:
            skipped_nonpositive += 1  # 출금·취소 행은 수납 대상이 아님
            continue
        base = f"{event.id}|{txn_at.isoformat()}|{row['depositor']}|{amount}|{row.get('memo', '')}"
        # 같은 파일 안의 똑같은 행(같은 시각 같은 금액 두 번)은 실제로 두 건일 수 있어 순번을 붙인다.
        occurrences[base] += 1
        row_hash = hashlib.sha256(f"{base}|{occurrences[base]}".encode()).hexdigest()
        parsed.append((txn_at, row["depositor"], amount, row.get("memo", ""), row_hash))
    if errors:
        raise invalid("CSV_ROWS", f"거래내역 {len(errors)}행에 오류가 있어 반영하지 않았습니다", errors[:50])

    existing = set(db.scalars(select(BankTransaction.row_hash).where(BankTransaction.event_id == event.id)))
    upload = Upload(event_id=event.id, kind="transactions", filename=filename[:200], sha256=hashlib.sha256(data).hexdigest(), row_count=len(parsed))
    db.add(upload)
    db.flush()
    inserted = 0
    for txn_at, depositor, amount, memo, row_hash in parsed:
        if row_hash in existing:
            continue
        db.add(BankTransaction(event_id=event.id, upload_id=upload.id, txn_at=txn_at, depositor=depositor, amount=amount, memo=memo, row_hash=row_hash))
        existing.add(row_hash)
        inserted += 1
    upload.inserted, upload.skipped = inserted, len(parsed) - inserted
    audit(db, event.id, actor, "transactions_uploaded", f"upload:{upload.id}", {"rows": len(parsed), "inserted": inserted})
    db.flush()
    return {"upload_id": upload.id, "rows": len(parsed), "inserted": inserted, "duplicates": len(parsed) - inserted, "ignored_nonpositive": skipped_nonpositive}


def allocated_sum(db: Session, transaction_id: int) -> int:
    return db.scalar(
        select(func.coalesce(func.sum(Allocation.amount), 0)).where(Allocation.transaction_id == transaction_id, Allocation.status != "cancelled")
    )


def get_transaction(db: Session, event: Event, transaction_id: int) -> BankTransaction:
    t = db.get(BankTransaction, transaction_id)
    if t is None or t.event_id != event.id:
        raise not_found("거래")
    return t


def allocate(
    db: Session,
    event: Event,
    transaction_id: int,
    team_id: int,
    amount: int,
    *,
    participant_id: int | None = None,
    status: str = "proposed",
    source: str,
    basis: str,
    confidence: str = "",
    actor: str,
    admin_id: int | None = None,
    now: datetime | None = None,
) -> Allocation:
    now = now or clock.now()
    if amount <= 0:
        raise invalid("INVALID_AMOUNT", "배정 금액은 0보다 커야 합니다")
    if status not in ("proposed", "confirmed"):
        raise invalid("INVALID_STATUS", "배정 상태는 proposed 또는 confirmed 입니다")
    txn = get_transaction(db, event, transaction_id)
    get_team_in_event(db, event.id, team_id)
    if participant_id is not None:
        p: Participant = get_participant_in_event(db, event.id, participant_id)
        if p.team_id != team_id:
            raise invalid("PARTICIPANT_TEAM_MISMATCH", "개인 입금은 그 참가자가 속한 팀으로만 배정할 수 있습니다")
    dup = db.scalar(
        select(Allocation).where(
            Allocation.transaction_id == txn.id,
            Allocation.team_id == team_id,
            func.coalesce(Allocation.participant_id, 0) == (participant_id or 0),
            Allocation.status != "cancelled",
        )
    )
    if dup is not None:
        raise conflict("DUPLICATE_ALLOCATION", "같은 거래를 같은 대상에 이미 배정했습니다", {"allocation_id": dup.id})
    used = allocated_sum(db, txn.id)
    if used + amount > txn.amount:
        raise conflict(
            "ALLOCATION_EXCEEDS_TRANSACTION",
            f"배정 합계가 거래 금액을 넘습니다(거래 {txn.amount:,}원, 이미 배정 {used:,}원, 요청 {amount:,}원)",
            {"transaction_amount": txn.amount, "allocated": used, "requested": amount},
        )
    alloc = Allocation(
        event_id=event.id,
        transaction_id=txn.id,
        team_id=team_id,
        participant_id=participant_id,
        amount=amount,
        status=status,
        source=source,
        basis=basis,
        confidence=confidence,
        created_at=now,
    )
    if status == "confirmed":
        alloc.confirmed_at, alloc.confirmed_by = now, admin_id
    db.add(alloc)
    try:
        db.flush()
    except IntegrityError as e:  # 동시 요청 등으로 앞의 검사를 통과한 경우의 마지막 방어선
        db.rollback()
        raise conflict("DUPLICATE_ALLOCATION", "같은 거래를 같은 대상에 이미 배정했습니다") from e
    audit(db, event.id, actor, "allocation_created", f"allocation:{alloc.id}", {"txn": txn.id, "team": team_id, "participant": participant_id, "amount": amount, "status": status})
    return alloc


def get_allocation(db: Session, event: Event, allocation_id: int) -> Allocation:
    a = db.get(Allocation, allocation_id)
    if a is None or a.event_id != event.id:
        raise not_found("배정")
    return a


def confirm_allocation(db: Session, event: Event, allocation_id: int, admin_id: int, now: datetime | None = None) -> Allocation:
    a = get_allocation(db, event, allocation_id)
    if a.status != "proposed":
        raise conflict("INVALID_STATE", f"제안 상태의 배정만 확정할 수 있습니다(현재 {a.status})")
    a.status, a.confirmed_at, a.confirmed_by = "confirmed", now or clock.now(), admin_id
    audit(db, event.id, f"admin:{admin_id}", "allocation_confirmed", f"allocation:{a.id}")
    return a


def cancel_allocation(db: Session, event: Event, allocation_id: int, actor: str, now: datetime | None = None) -> Allocation:
    a = get_allocation(db, event, allocation_id)
    if a.status == "cancelled":
        raise conflict("INVALID_STATE", "이미 취소된 배정입니다")
    a.status, a.cancelled_at = "cancelled", now or clock.now()
    audit(db, event.id, actor, "allocation_cancelled", f"allocation:{a.id}")
    return a


def team_received(db: Session, event_id: int, team_id: int, statuses: tuple[str, ...] = ("confirmed",)) -> int:
    return db.scalar(
        select(func.coalesce(func.sum(Allocation.amount), 0)).where(
            Allocation.event_id == event_id, Allocation.team_id == team_id, Allocation.status.in_(statuses)
        )
    )
