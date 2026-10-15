"""명단 CSV 업로드: 전체 명단 한 파일 → 소속 부서별로 팀을 만들고 팀 담당자(팀장)를 지정한다.

열(첫 줄 이름, 순서 무관): ID, 이름, 전화번호, Slack ID, 소속 부서, 팀 담당자
- 소속 부서 하나 = 팀 하나. 팀코드(T01, T02 …)는 처음 나온 순서대로 자동으로 붙인다(이미 있는 부서는 코드 유지).
- 팀 담당자 열에는 그 부서 팀장의 ID 나 이름을 적는다(행마다 같은 값). ID 로 먼저 찾고, 없으면 같은 부서 안에서 이름으로 찾는다.
  같은 부서에 다른 값이 섞여 있거나, 이름이 동명이인이거나, 그 부서에 없는 사람이면 오류로 반려한다.
  비어 있으면 담당자 미지정(경고)으로 올리고 총무가 화면에서 지정한다.
- 한 행이라도 오류가 있으면 아무것도 반영하지 않는다.
연락은 Slack DM 으로 하므로 이메일 열은 없다.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..errors import invalid
from ..models import Event, Participant, Team, TeamManager, Upload
from .common import active_manager, audit

HEADER_MAP = {
    "id": "employee_no", "ID": "employee_no", "아이디": "employee_no", "사번": "employee_no", "employee_no": "employee_no",
    "이름": "name", "성명": "name", "name": "name",
    "전화번호": "phone", "연락처": "phone", "휴대폰": "phone", "phone": "phone",
    "slack id": "slack_id", "slackid": "slack_id", "slack_id": "slack_id", "슬랙 id": "slack_id", "슬랙id": "slack_id",
    "소속 부서": "department", "소속부서": "department", "부서": "department", "department": "department",
    "팀 담당자": "manager", "팀담당자": "manager", "팀 담당자(팀장)": "manager", "팀장": "manager", "담당자": "manager", "manager": "manager",
}
REQUIRED = ("employee_no", "name", "slack_id", "department")
COLUMN_LABEL = {"employee_no": "ID", "name": "이름", "phone": "전화번호", "slack_id": "Slack ID", "department": "소속 부서", "manager": "팀 담당자"}
SLACK_ID_RE = re.compile(r"^[UW][A-Z0-9]{6,20}$")


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp949"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise invalid("CSV_ENCODING", "CSV 인코딩을 읽을 수 없습니다(UTF-8 또는 CP949)")


def _key(header: str | None) -> str | None:
    h = (header or "").strip()
    return HEADER_MAP.get(h) or HEADER_MAP.get(h.lower())


def parse_roster(data: bytes) -> tuple[list[dict], dict[str, str | None], list[dict], list[str]]:
    """검사만 한다. 반환: (행들, 부서별 담당자 ID, 오류, 경고)."""
    reader = csv.DictReader(io.StringIO(_decode(data)))
    cols = {_key(h) for h in reader.fieldnames or []}
    missing = [COLUMN_LABEL[c] for c in (*REQUIRED, "manager") if c not in cols]
    if missing:
        raise invalid("CSV_COLUMNS", f"명단 CSV 에 필요한 열이 없습니다: {', '.join(missing)} (필요한 열: ID, 이름, 전화번호, Slack ID, 소속 부서, 팀 담당자)")
    rows: list[dict] = []
    errors: list[dict] = []
    seen_id: dict[str, int] = {}
    seen_slack: dict[str, int] = {}
    for line, raw in enumerate(reader, start=2):
        row = {"phone": "", "manager": ""}
        for k, v in raw.items():
            key = _key(k)
            if key:
                row[key] = (v or "").strip()
        row["slack_id"] = row.get("slack_id", "").upper()
        if not any(row.get(c) for c in REQUIRED):
            continue  # 빈 줄
        empty = [COLUMN_LABEL[c] for c in REQUIRED if not row.get(c)]
        if empty:
            errors.append({"line": line, "error": f"빈 값: {', '.join(empty)}"})
        elif not SLACK_ID_RE.match(row["slack_id"]):
            errors.append({"line": line, "error": f"Slack ID 형식 오류: {row['slack_id']} (예: U01AB2CD3EF — Slack 프로필 ⋮ → 멤버 ID 복사)"})
        elif row["employee_no"] in seen_id:
            errors.append({"line": line, "error": f"ID {row['employee_no']}이(가) {seen_id[row['employee_no']]}행과 중복"})
        elif row["slack_id"] in seen_slack:
            errors.append({"line": line, "error": f"Slack ID {row['slack_id']}이(가) {seen_slack[row['slack_id']]}행과 중복"})
        else:
            seen_id[row["employee_no"]] = line
            seen_slack[row["slack_id"]] = line
            rows.append(row | {"line": line})

    # 부서별 팀 담당자 확인
    managers: dict[str, str | None] = {}
    warnings: list[str] = []
    by_dept: dict[str, list[dict]] = {}
    for r in rows:
        by_dept.setdefault(r["department"], []).append(r)
    for dept, members in by_dept.items():
        values = sorted({r["manager"] for r in members if r["manager"]})
        if not values:
            managers[dept] = None
            warnings.append(f"{dept}: 팀 담당자가 비어 있어 미지정으로 올립니다(화면에서 지정)")
            continue
        if len(values) > 1:
            errors.append({"line": members[0]["line"], "error": f"{dept}: 팀 담당자가 여러 명으로 적혀 있습니다({', '.join(values)}) — 한 명만 적어 주세요"})
            continue
        ref = values[0]
        hit = [r for r in members if r["employee_no"] == ref] or [r for r in members if r["name"] == ref]
        if not hit:
            errors.append({"line": members[0]["line"], "error": f"{dept}: 팀 담당자로 적힌 '{ref}'이(가) 이 부서 명단에 없습니다"})
        elif len(hit) > 1:
            errors.append({"line": members[0]["line"], "error": f"{dept}: 팀 담당자로 적힌 '{ref}'과(와) 이름이 같은 사람이 {len(hit)}명 — ID 로 적어 주세요"})
        else:
            managers[dept] = hit[0]["employee_no"]
        blanks = sum(1 for r in members if not r["manager"])
        if blanks and dept in managers:
            warnings.append(f"{dept}: {blanks}행의 팀 담당자 칸이 비어 있어 같은 부서의 값({ref})을 썼습니다")
    return rows, managers, errors, warnings


def import_roster(db: Session, event: Event, data: bytes, filename: str, actor: str, admin_id: int | None = None) -> dict:
    rows, managers, errors, warnings = parse_roster(data)
    if errors:
        raise invalid("CSV_ROWS", f"명단 {len(errors)}건에 오류가 있어 반영하지 않았습니다", sorted(errors, key=lambda e: e["line"])[:50])
    if not rows:
        raise invalid("CSV_EMPTY", "명단에 사람이 없습니다")
    if admin_id is None and actor.startswith("admin:"):
        admin_id = int(actor.split(":", 1)[1])

    teams = {t.name: t for t in db.scalars(select(Team).where(Team.event_id == event.id))}
    used_codes = {t.code for t in teams.values()}
    next_no = 1

    def new_code() -> str:
        nonlocal next_no
        while f"T{next_no:02d}" in used_codes:
            next_no += 1
        used_codes.add(f"T{next_no:02d}")
        return f"T{next_no:02d}"

    existing = {p.employee_no: p for p in db.scalars(select(Participant).where(Participant.event_id == event.id))}
    inserted = updated = 0
    by_no: dict[str, Participant] = {}
    for row in rows:
        team = teams.get(row["department"])
        if team is None:
            team = Team(event_id=event.id, code=new_code(), name=row["department"])
            db.add(team)
            db.flush()
            teams[team.name] = team
        p = existing.get(row["employee_no"])
        values = {"team_id": team.id, "name": row["name"], "slack_id": row["slack_id"], "phone": row["phone"]}
        if p is None:
            p = Participant(event_id=event.id, employee_no=row["employee_no"], **values)
            db.add(p)
            inserted += 1
        elif any(getattr(p, k) != v for k, v in values.items()):
            for k, v in values.items():
                setattr(p, k, v)
            updated += 1
        by_no[row["employee_no"]] = p
    db.flush()

    assigned = 0
    for dept, emp_no in managers.items():
        if emp_no is None or admin_id is None:
            continue
        team, p = teams[dept], by_no[emp_no]
        current = active_manager(db, team.id)
        if current and current.participant_id == p.id:
            continue
        if current:
            current.revoked_at = clock.now()
            db.flush()
        db.add(TeamManager(team_id=team.id, participant_id=p.id, assigned_by=admin_id))
        assigned += 1
    db.flush()

    depts = []
    for dept in dict.fromkeys(r["department"] for r in rows):
        t = teams[dept]
        m = active_manager(db, t.id)
        mp = db.get(Participant, m.participant_id) if m else None
        depts.append({"code": t.code, "name": t.name, "headcount": sum(1 for r in rows if r["department"] == dept), "manager": mp.name if mp else None})
    db.add(Upload(event_id=event.id, kind="roster", filename=filename[:200], sha256=hashlib.sha256(data).hexdigest(), row_count=len(rows), inserted=inserted, skipped=len(rows) - inserted - updated))
    audit(db, event.id, actor, "roster_uploaded", detail={"rows": len(rows), "inserted": inserted, "updated": updated, "departments": len(depts), "managers_assigned": assigned})
    db.flush()
    # 지금 담당자가 있는 부서 수(이번 업로드로 새로 지정한 수가 아님). 같은 명단을 다시 올려도 34 처럼 그대로 보인다.
    managers_total = sum(1 for t in teams.values() if active_manager(db, t.id))
    return {"rows": len(rows), "inserted": inserted, "updated": updated, "teams": len(depts), "managers_assigned": assigned, "managers_total": managers_total, "departments": depts, "warnings": warnings}
