"""시연용 응답·변경 CSV 넣기(APP_MODE=demo 전용). 실제 운영에서는 참가자가 직접 응답하고 총무가 승인한다.

순서는 실제로 일어난 것과 같게 재현한다(seed.py 와 같은 방식):
  응답(응답일시마다 clock 고정 → submit_change, source=web) → 참석 마감 기준 응답 저장(ensure_snapshot)
  → 변경(요청·처리 시각 순서대로 submit_change / decide_change, 납부 마감을 지나는 순간 청구표 저장 ensure_billing)
  → clock 원래대로 → 정산 보고서 다시 만들기.
같은 파일을 다시 넣어도 결과가 같도록, 넣을 때마다 그 행사의 응답·변경 기록과 기준표·보고서를 지우고 처음부터 다시 만든다.
변경 CSV 는 마지막에 넣은 응답 CSV(db_meta 에 보관) 위에 다시 쌓는다. 한 행이라도 틀리면 아무것도 반영하지 않는다.
"""
from __future__ import annotations

import csv
import io
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import clock
from ..errors import AppError, invalid
from ..models import CHOICE_LABELS, Admin, ChangeLog, ConfirmTask, DbMeta, DeadlineSnapshot, Event, Participant, Response, SettlementReport, TeamBilling
from . import report, responses
from .common import audit
from .roster import _decode

CHOICE_KO = {"참석+버스": "bus", "참석+자차": "car", "불참": "absent", "미정": "undecided", "": None, "미응답": None}
SOURCE_KO = {"참가자 Agent 채팅": "chat", "참가자 응답": "web", "총무 직접 입력": "admin", "행사 당일(총무 입력)": "admin"}
DECISION_KO = {"승인": "approve", "반려": "reject", "승인 대기": None, "": None}
RESP_COLS = ("ID", "응답", "응답일시")
CHANGE_COLS = ("ID", "변경 전", "변경 후", "요청일시", "경로", "처리")


def _meta_key(event: Event) -> str:
    return f"demo_responses:{event.id}"


def _rows(data: bytes, required: tuple[str, ...]) -> list[tuple[int, dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(_decode(data)))
    header = [h.strip() for h in (reader.fieldnames or [])]
    missing = [c for c in required if c not in header]
    if missing:
        raise invalid("CSV_COLUMNS", f"열이 없습니다: {', '.join(missing)}", [{"line": 1, "error": f"필요한 열: {', '.join(required)}"}])
    out = []
    for i, raw in enumerate(reader, start=2):
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        if any(row.values()):
            out.append((i, row))
    return out


def _when(value: str) -> datetime:
    """'2026-10-05 18:00' (KST) → naive UTC"""
    return clock.parse_input(value.replace(" ", "T") if "T" not in value else value)


def _people(db: Session, event: Event) -> dict[str, Participant]:
    return {p.employee_no: p for p in db.scalars(select(Participant).where(Participant.event_id == event.id))}


def _check_person(row: dict[str, str], people: dict[str, Participant], teams: dict[int, str]) -> str | None:
    p = people.get(row["ID"])
    if p is None:
        return f"명단에 없는 ID: {row['ID'] or '(빈칸)'}"
    if row.get("이름") and row["이름"] != p.name:
        return f"{row['ID']} 이름이 명단과 다릅니다(명단: {p.name}, CSV: {row['이름']})"
    if row.get("소속 부서") and row["소속 부서"] != teams[p.team_id]:
        return f"{row['ID']} 소속 부서가 명단과 다릅니다(명단: {teams[p.team_id]}, CSV: {row['소속 부서']})"
    return None


def _teams(db: Session, event: Event) -> dict[int, str]:
    from ..models import Team

    return {t.id: t.name for t in db.scalars(select(Team).where(Team.event_id == event.id))}


def parse_responses(db: Session, event: Event, data: bytes) -> list[dict[str, Any]]:
    people, teams = _people(db, event), _teams(db, event)
    rows, errors, seen = [], [], set()
    for line, row in _rows(data, RESP_COLS):
        err = _check_person(row, people, teams)
        if err is None and row["ID"] in seen:
            err = f"{row['ID']} 이(가) 두 번 나옵니다"
        if err is None and row["응답"] not in CHOICE_KO:
            err = f"응답 값 오류: '{row['응답']}' (참석+버스 / 참석+자차 / 불참 / 미정 / 빈칸=미응답)"
        at = None
        if err is None and CHOICE_KO[row["응답"]] is not None:
            try:
                at = _when(row["응답일시"])
            except ValueError:
                err = f"응답일시 형식 오류: '{row['응답일시']}' (예: 2026-10-02 18:51)"
            else:
                if at >= event.attend_deadline:
                    err = f"응답일시가 참석 마감({clock.fmt(event.attend_deadline)}) 이후입니다 — 마감 후 바뀐 것은 변경 CSV 로 넣으세요"
        if err:
            errors.append({"line": line, "error": err})
            continue
        seen.add(row["ID"])
        rows.append({"line": line, "participant": people[row["ID"]], "choice": CHOICE_KO[row["응답"]], "at": at})
    if errors:
        raise invalid("CSV_ROWS", f"응답 {len(errors)}건에 오류가 있어 반영하지 않았습니다", errors[:50])
    if not rows:
        raise invalid("CSV_EMPTY", "응답 CSV 에 사람이 없습니다")
    return rows


def parse_changes(db: Session, event: Event, data: bytes) -> list[dict[str, Any]]:
    people, teams = _people(db, event), _teams(db, event)
    rows, errors = [], []
    for line, row in _rows(data, CHANGE_COLS):
        err = _check_person(row, people, teams)
        if err is None:
            for col in ("변경 전", "변경 후"):
                if row[col] not in CHOICE_KO:
                    err = f"{col} 값 오류: '{row[col]}'"
            if err is None and CHOICE_KO[row["변경 후"]] is None:
                err = "변경 후는 참석+버스 / 참석+자차 / 불참 / 미정 중 하나여야 합니다"
        if err is None and row["경로"] not in SOURCE_KO:
            err = f"경로 값 오류: '{row['경로']}' ({' / '.join(SOURCE_KO)})"
        if err is None and row["처리"] not in DECISION_KO:
            err = f"처리 값 오류: '{row['처리']}' (승인 / 반려 / 승인 대기)"
        req = dec = refund = None
        if err is None:
            try:
                req = _when(row["요청일시"])
                if DECISION_KO[row["처리"]]:
                    dec = _when(row.get("처리일시", ""))
            except ValueError:
                err = "요청일시·처리일시 형식 오류 (예: 2026-10-09 09:40, 승인·반려는 처리일시 필수)"
            if err is None and dec is not None and dec < req:
                err = "처리일시가 요청일시보다 앞섭니다"
        if err is None and row.get("환불액(규칙 없을 때)", ""):
            try:
                refund = int(row["환불액(규칙 없을 때)"].replace(",", "").replace("원", ""))
            except ValueError:
                err = f"환불액 값 오류: '{row['환불액(규칙 없을 때)']}'"
        if err:
            errors.append({"line": line, "error": err})
            continue
        rows.append({
            "line": line, "participant": people[row["ID"]], "from": CHOICE_KO[row["변경 전"]], "to": CHOICE_KO[row["변경 후"]],
            "requested_at": req, "decided_at": dec, "decision": DECISION_KO[row["처리"]], "source": SOURCE_KO[row["경로"]],
            "refund": refund, "note": row.get("메모", ""),
        })
    if errors:
        raise invalid("CSV_ROWS", f"변경 {len(errors)}건에 오류가 있어 반영하지 않았습니다", errors[:50])
    if not rows:
        raise invalid("CSV_EMPTY", "변경 CSV 에 행이 없습니다")
    return rows


def _reset(db: Session, event: Event) -> None:
    """그 행사의 응답·변경 기록과 마감 기준표·청구표·보고서를 지운다(입금 기록·배정은 그대로)."""
    pids = select(Participant.id).where(Participant.event_id == event.id)
    db.execute(delete(ConfirmTask).where(ConfirmTask.event_id == event.id, ConfirmTask.kind == "change_approval"))
    db.execute(delete(ChangeLog).where(ChangeLog.event_id == event.id))
    db.execute(delete(Response).where(Response.participant_id.in_(pids)))
    db.execute(delete(DeadlineSnapshot).where(DeadlineSnapshot.event_id == event.id))
    db.execute(delete(TeamBilling).where(TeamBilling.event_id == event.id))
    db.execute(delete(SettlementReport).where(SettlementReport.event_id == event.id))
    event.snapshot_taken_at = None
    db.flush()


def _apply_responses(db: Session, event: Event, rows: list[dict[str, Any]], real_now: datetime) -> None:
    for r in sorted((r for r in rows if r["choice"] is not None), key=lambda r: (r["at"], r["line"])):
        clock.freeze(r["at"])
        p = r["participant"]
        responses.submit_change(db, event, p, r["choice"], source="web", actor=f"participant:{p.id}")
    if real_now >= event.attend_deadline:
        responses.ensure_snapshot(db, event, now=event.attend_deadline)


def _summary(db: Session, event: Event) -> dict[str, Any]:
    pids = list(db.scalars(select(Participant.id).where(Participant.event_id == event.id)))
    snaps = {s.participant_id: s.choice for s in db.scalars(select(DeadlineSnapshot).where(DeadlineSnapshot.event_id == event.id))}
    current = {r.participant_id: r.choice for r in db.scalars(select(Response).where(Response.participant_id.in_(pids)))}
    base = snaps if event.snapshot_taken_at else current
    c = Counter(base.get(pid) or "none" for pid in pids)
    changes = Counter(ch.status for ch in db.scalars(select(ChangeLog).where(ChangeLog.event_id == event.id, ChangeLog.after_deadline.is_(True))))
    return {
        "headcount": len(pids), "basis": "참석 마감 기준" if event.snapshot_taken_at else "현재 응답",
        "counts": {k: c.get(k, 0) for k in ("bus", "car", "absent", "undecided", "none")},
        "attending_now": sum(1 for pid in pids if current.get(pid) in ("bus", "car")),
        "changes": {"applied": changes.get("applied", 0), "pending": changes.get("pending_approval", 0), "rejected": changes.get("rejected", 0)},
    }


def _run(db: Session, event: Event, actor: str, trigger: str, fn) -> dict[str, Any]:
    prev = clock._frozen
    real_now = clock.now()
    try:
        _reset(db, event)
        fn(real_now)
    finally:
        clock.freeze(prev)
    report.refresh(db, event, trigger)
    out = _summary(db, event)
    audit(db, event.id, actor, trigger, detail={"counts": out["counts"], "changes": out["changes"]})
    return out


def import_responses(db: Session, event: Event, admin: Admin, data: bytes) -> dict[str, Any]:
    rows = parse_responses(db, event, data)
    out = _run(db, event, f"admin:{admin.id}", "demo_responses", lambda now: _apply_responses(db, event, rows, now))
    db.merge(DbMeta(key=_meta_key(event), value=_decode(data)))
    return out


def import_changes(db: Session, event: Event, admin: Admin, data: bytes) -> dict[str, Any]:
    stored = db.get(DbMeta, _meta_key(event))
    if stored is None:
        raise invalid("RESPONSES_FIRST", "응답 CSV 를 먼저 넣으세요(변경은 마감 기준 응답 위에 쌓습니다)")
    resp_rows = parse_responses(db, event, stored.value.encode("utf-8"))
    rows = parse_changes(db, event, data)

    def apply(real_now: datetime) -> None:
        _apply_responses(db, event, resp_rows, real_now)
        steps = [(r["requested_at"], 0, r["line"], r) for r in rows] + [(r["decided_at"], 1, r["line"], r) for r in rows if r["decision"]]
        billed = False
        errors: list[dict[str, Any]] = []
        for at, kind, line, r in sorted(steps, key=lambda s: s[:3]):
            if not billed and at >= event.payment_deadline:
                report.ensure_billing(db, event, now=event.payment_deadline + timedelta(minutes=1))
                billed = True
            clock.freeze(at)
            p = r["participant"]
            try:
                if kind == 0:
                    current = responses.current_choice(db, p.id)
                    if current != r["from"]:
                        raise invalid("FROM_MISMATCH", f"변경 전이 '{CHOICE_LABELS[r['from']]}'인데 그 시각 응답은 '{CHOICE_LABELS[current]}'입니다")
                    actor = f"admin:{admin.id}" if r["source"] == "admin" else f"participant:{p.id}"
                    r["change"] = responses.submit_change(db, event, p, r["to"], source=r["source"], actor=actor, note=r["note"])
                elif "change" in r:
                    if r["change"].status != "pending_approval":
                        continue  # 참석 마감 전 변경은 바로 반영돼 승인할 것이 없음
                    note = (r["note"] or "시연 데이터") if r["refund"] is not None else ""
                    responses.decide_change(db, event, r["change"], admin, approve=r["decision"] == "approve", note=note, refund_amount=r["refund"] if r["decision"] == "approve" else None)
            except AppError as e:
                errors.append({"line": line, "error": f"{p.name}: {e.message}"})
        if errors:
            raise invalid("CSV_ROWS", f"변경 {len(errors)}건을 반영할 수 없어 아무것도 바꾸지 않았습니다", errors[:50])
        if not billed and real_now >= event.payment_deadline:
            report.ensure_billing(db, event, now=event.payment_deadline + timedelta(minutes=1))

    return _run(db, event, f"admin:{admin.id}", "demo_changes", apply)
