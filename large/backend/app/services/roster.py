"""명단 CSV 업로드. 열: 사번,이름,이메일,팀코드,팀명 (또는 employee_no,name,email,team_code,team_name)."""
from __future__ import annotations

import csv
import hashlib
import io
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import invalid
from ..models import Event, Participant, Team, Upload
from .common import audit

HEADER_MAP = {
    "사번": "employee_no", "employee_no": "employee_no",
    "이름": "name", "name": "name",
    "이메일": "email", "email": "email",
    "팀코드": "team_code", "team_code": "team_code",
    "팀명": "team_name", "team_name": "team_name",
}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp949"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise invalid("CSV_ENCODING", "CSV 인코딩을 읽을 수 없습니다(UTF-8 또는 CP949)")


def import_roster(db: Session, event: Event, data: bytes, filename: str, actor: str) -> dict:
    reader = csv.DictReader(io.StringIO(_decode(data)))
    cols = {HEADER_MAP.get((h or "").strip()) for h in reader.fieldnames or []}
    missing = {"employee_no", "name", "email", "team_code", "team_name"} - cols
    if missing:
        raise invalid("CSV_COLUMNS", f"명단 CSV 에 필요한 열이 없습니다: {sorted(missing)}")
    rows, errors, seen_emails = [], [], set()
    for i, raw in enumerate(reader, start=2):
        row = {HEADER_MAP[k.strip()]: (v or "").strip() for k, v in raw.items() if k and k.strip() in HEADER_MAP}
        email = row["email"].lower()
        if not all(row.values()):
            errors.append({"line": i, "error": "빈 값이 있습니다"})
        elif not EMAIL_RE.match(email):
            errors.append({"line": i, "error": "이메일 형식 오류"})
        elif email in seen_emails:
            errors.append({"line": i, "error": "파일 안에서 이메일 중복"})
        else:
            seen_emails.add(email)
            rows.append(row | {"email": email})
    if errors:
        raise invalid("CSV_ROWS", f"명단 {len(errors)}행에 오류가 있어 반영하지 않았습니다", errors[:50])

    teams = {t.code: t for t in db.scalars(select(Team).where(Team.event_id == event.id))}
    existing = {p.email: p for p in db.scalars(select(Participant).where(Participant.event_id == event.id))}
    inserted = updated = 0
    for row in rows:
        team = teams.get(row["team_code"])
        if team is None:
            team = Team(event_id=event.id, code=row["team_code"], name=row["team_name"])
            db.add(team)
            db.flush()
            teams[team.code] = team
        p = existing.get(row["email"])
        if p is None:
            db.add(Participant(event_id=event.id, team_id=team.id, employee_no=row["employee_no"], name=row["name"], email=row["email"]))
            inserted += 1
        elif (p.team_id, p.name, p.employee_no) != (team.id, row["name"], row["employee_no"]):
            p.team_id, p.name, p.employee_no = team.id, row["name"], row["employee_no"]
            updated += 1
    db.add(Upload(event_id=event.id, kind="roster", filename=filename[:200], sha256=hashlib.sha256(data).hexdigest(), row_count=len(rows), inserted=inserted, skipped=len(rows) - inserted - updated))
    audit(db, event.id, actor, "roster_uploaded", detail={"rows": len(rows), "inserted": inserted, "updated": updated})
    db.flush()
    return {"rows": len(rows), "inserted": inserted, "updated": updated, "teams": len(teams)}
