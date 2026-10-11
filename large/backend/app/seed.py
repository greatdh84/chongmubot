"""재현 가능한 가상 데이터: 300명 · 25개 팀 + 시연 시나리오.

    python -m app.seed --reset                    # 오늘(KST) 기준 날짜로 생성
    python -m app.seed --reset --base-date 2026-10-08   # 날짜까지 완전히 고정

같은 --seed / --base-date 면 항상 같은 데이터가 나온다. 모든 이름·이메일은 가상이며 이메일은 example.com.

시나리오(팀코드 · 팀 내 순번, 0번은 담당자)
- T02#7 버스→불참 변경 요청(총무 승인 대기)       - T09#3 자차→불참 변경 요청(승인 대기)
- T03#3 버스→불참(입금 마감 전, 전액 환불)         - T12#4 버스→불참(입금 마감 후, 50% 환불)
- T07#5 버스→자차(차액 환불) / T07#8 자차→버스(추가 납부)
- T15#6 버스→자차(입금 마감 후, 환불 없음)          - T11#2 불참→버스(추가 납부), T04#9 미정→버스
- T05#4 · T17#9 동명이인 '김민준' + 입금자 '김민준' 20,000원(팀 불명)
- T08#10 개인 직접 입금(팀 일괄 입금에서 빠짐)       - T13#6 개인 입금 + 팀 일괄 입금 모두(중복 가능성)
- T16 입금자 'ㅎㅂ'(불명) — 미납 T19 와 금액이 같아 금액만으로는 팀을 정할 수 없음
- T06 메모 없는 담당자 명의 입금, T11 팀명 메모, T22 2회 분할 입금, T24 미납(2차 CSV 로 입금), T25 담당자 미지정
- '결산이자' 37원(회비와 무관한 입금) → 총무 이관
- emp150 메일은 반송(발송 실패·재시도 시연)
"""
from __future__ import annotations

import argparse
import csv
import io
import random
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from . import clock, db as dbmod
from .config import BACKEND_DIR, settings
from .models import Admin, Event, Participant, RefundRule, Team, TeamManager
from .security import hash_password
from .services import emails, payments, responses, roster

TEAM_NAMES = [
    "경영지원팀", "재무팀", "인사팀", "총무팀", "법무팀", "전략기획팀", "마케팅팀", "브랜드팀", "영업1팀", "영업2팀",
    "해외영업팀", "고객지원팀", "품질관리팀", "생산기술팀", "구매팀", "물류팀", "연구1팀", "연구2팀", "플랫폼개발팀",
    "모바일개발팀", "데이터팀", "보안팀", "인프라팀", "디자인팀", "홍보팀",
]
SURNAMES = ["김", "이", "박", "최", "정", "강", "조", "윤", "장", "임", "한", "오", "서", "신", "권", "황", "안", "송", "류", "전"]
GIVEN = [
    "민준", "서준", "도윤", "예준", "시우", "하준", "주원", "지호", "지후", "준우", "서연", "서윤", "지우", "하은", "민서", "하윤",
    "윤서", "지민", "채원", "수아", "지아", "다은", "은서", "예은", "현우", "건우", "우진", "선우", "유준", "연우", "정우", "승우",
    "시윤", "지유", "소율", "예린", "수빈", "지원", "민재", "태윤",
]
TEAM_SIZE = 12
SAMPLES_DIR = BACKEND_DIR / "var" / "samples"


def kst(d: date, days: float = 0, hour: int = 10, minute: int = 0) -> datetime:
    """기준일 + days 의 KST 시각 → naive UTC."""
    local = datetime(d.year, d.month, d.day, hour, minute) + timedelta(days=days)
    return clock.kst_naive_to_utc(local)


def build_roster(rng: random.Random) -> list[dict]:
    pool = [s + g for s in SURNAMES for g in GIVEN if s + g != "김민준"]
    names = rng.sample(pool, 25 * TEAM_SIZE)
    rows = []
    n = 0
    for ti, team_name in enumerate(TEAM_NAMES, start=1):
        for mi in range(TEAM_SIZE):
            n += 1
            name = names[n - 1]
            if (ti, mi) in ((5, 4), (17, 9)):
                name = "김민준"  # 동명이인 시나리오
            email = f"emp{n:03d}@example.com" if n != 150 else "emp150.bounce@example.com"
            rows.append({"사번": f"E26{n:04d}", "이름": name, "이메일": email, "팀코드": f"T{ti:02d}", "팀명": team_name})
    return rows


def snapshot_choices(rng: random.Random) -> dict[tuple[int, int], str | None]:
    out: dict[tuple[int, int], str | None] = {}
    for ti in range(1, 26):
        for mi in range(TEAM_SIZE):
            r = rng.random()
            out[(ti, mi)] = "bus" if r < 0.64 else "car" if r < 0.86 else "absent" if r < 0.97 else "undecided" if r < 0.985 else None
            if mi == 0:
                out[(ti, mi)] = "bus"  # 담당자는 응답 완료
    for ti in (16, 19):  # 금액이 같은 두 팀(입금자 불명 시나리오)
        for mi, c in enumerate(["bus"] * 9 + ["car"] * 2 + ["absent"]):
            out[(ti, mi)] = c
    forced = {(2, 7): "bus", (3, 3): "bus", (4, 9): "undecided", (5, 4): "bus", (7, 5): "bus", (7, 8): "car", (8, 10): "bus",
              (9, 3): "car", (11, 2): "absent", (12, 4): "bus", (13, 6): "bus", (15, 6): "bus", (17, 9): "bus"}
    out.update(forced)
    return out


def to_csv(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def seed(base: date, rng_seed: int = 20261015) -> dict:
    rng = random.Random(rng_seed)
    dbmod.init_db()
    db = dbmod.SessionLocal()
    try:
        admin = db.scalar(select(Admin).where(Admin.email == settings.admin_email.lower()))
        if admin is None:
            admin = Admin(email=settings.admin_email.lower(), password_hash=hash_password(settings.admin_password))
            db.add(admin)
            db.flush()

        attend_deadline = kst(base, -8, 18)
        payment_deadline = kst(base, -1, 18)
        event_at = kst(base, 5, 9)
        clock.freeze(kst(base, -20))
        ev = Event(
            admin_id=admin.id, name="2026 가을 전사 워크숍", event_at=event_at, attend_deadline=attend_deadline, payment_deadline=payment_deadline,
            fee_bus=20000, fee_car=15000, fee_absent=0, account_note="(시연) 가상은행 000-000000-00 워크숍 총무",
        )
        db.add(ev)
        db.flush()
        for ctype, until, pct, desc in [
            ("attend_to_absent", payment_deadline, 100, "입금 마감 전 불참 전환: 전액 환불"),
            ("attend_to_absent", event_at - timedelta(days=3), 50, "입금 마감 후 ~ 행사 3일 전 불참: 50% 환불"),
            ("bus_to_car", payment_deadline, 100, "입금 마감 전 버스→자차: 차액 전액 환불"),
            ("bus_to_car", None, 0, "입금 마감 후 버스→자차: 버스 좌석 확정으로 환불 없음"),
        ]:
            db.add(RefundRule(event_id=ev.id, change_type=ctype, valid_until=until, refund_percent=pct, description=desc))

        rows = build_roster(rng)
        SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
        (SAMPLES_DIR / "roster_synthetic.csv").write_bytes(to_csv(rows))
        roster.import_roster(db, ev, to_csv(rows), "roster_synthetic.csv", f"admin:{admin.id}")

        teams = {t.code: t for t in db.scalars(select(Team).where(Team.event_id == ev.id))}
        members: dict[int, list[Participant]] = {}
        for p in db.scalars(select(Participant).where(Participant.event_id == ev.id).order_by(Participant.employee_no)):
            members.setdefault(int(db.get(Team, p.team_id).code[1:]), []).append(p)

        def member(ti: int, mi: int) -> Participant:
            return members[ti][mi]

        for ti in range(1, 25):  # T25 는 담당자 미지정
            db.add(TeamManager(team_id=teams[f"T{ti:02d}"].id, participant_id=member(ti, 0).id, assigned_by=admin.id, assigned_at=kst(base, -20)))

        # 초대 메일(개발용 수신함) — 발송 시점에 토큰 발급
        emails.enqueue_invites(db, ev)
        emails.dispatch_due(db, ev, now=kst(base, -20))

        # 참석 마감 전 응답
        choices = snapshot_choices(rng)
        for (ti, mi), choice in sorted(choices.items()):
            if choice is None:
                continue
            p = member(ti, mi)
            first = "car" if (ti, mi) in ((1, 3), (6, 2)) else choice  # 마감 전에 한 번 바꾼 사람
            clock.freeze(kst(base, -18 + rng.random() * 9))
            responses.submit_change(db, ev, p, first, source="web", actor=f"participant:{p.id}")
            if first != choice:
                clock.freeze(kst(base, -8.5))
                responses.submit_change(db, ev, p, choice, source="web", actor=f"participant:{p.id}")

        # 참석 마감 → 마감 기준 응답 저장
        responses.ensure_snapshot(db, ev, now=attend_deadline)

        # 마감 후 변경(총무 승인 완료)
        approved = [((3, 3), "absent", kst(base, -3)), ((7, 5), "car", kst(base, -4)), ((7, 8), "bus", kst(base, -2)),
                    ((11, 2), "bus", kst(base, -5)), ((4, 9), "bus", kst(base, -6)), ((12, 4), "absent", kst(base, -0.5)),
                    ((15, 6), "car", kst(base, -0.25))]
        for (ti, mi), to, at in approved:
            clock.freeze(at)
            ch = responses.submit_change(db, ev, member(ti, mi), to, source="chat", actor=f"participant:{member(ti, mi).id}")
            clock.freeze(at + timedelta(hours=2))
            responses.decide_change(db, ev, ch, admin, approve=True, note="시드: 총무 승인")
        for (ti, mi), to, at in [((2, 7), "absent", kst(base, -0.4)), ((9, 3), "absent", kst(base, -0.35))]:
            clock.freeze(at)
            responses.submit_change(db, ev, member(ti, mi), to, source="chat", actor=f"participant:{member(ti, mi).id}")

        # 거래내역 1차 (입금 마감 전후)
        snap_fee = {(ti, mi): (ev.fee_bus if c == "bus" else ev.fee_car if c == "car" else 0) for (ti, mi), c in choices.items()}

        def lump(ti: int, exclude: tuple[int, ...] = ()) -> int:
            return sum(snap_fee[(ti, mi)] for mi in range(TEAM_SIZE) if mi not in exclude)

        def when(day: float) -> str:
            return clock.kst(kst(base, day, 9 + int(rng.random() * 9), int(rng.random() * 60))).strftime("%Y-%m-%d %H:%M")

        tx: list[dict] = []
        for ti in range(1, 26):
            code, mgr = f"T{ti:02d}", member(ti, 0).name
            if ti in (16, 19, 24):
                continue
            if ti == 5:
                tx.append({"거래일시": when(-6), "입금자명": mgr, "금액": lump(5, (4,)), "메모": f"{code} 회비"})
            elif ti == 6:
                tx.append({"거래일시": when(-5), "입금자명": mgr, "금액": lump(6), "메모": ""})
            elif ti == 8:
                tx.append({"거래일시": when(-4), "입금자명": mgr, "금액": lump(8, (10,)), "메모": f"{code} 워크숍"})
                tx.append({"거래일시": when(-3), "입금자명": member(8, 10).name, "금액": ev.fee_bus, "메모": ""})
            elif ti == 11:
                tx.append({"거래일시": when(-4), "입금자명": mgr, "금액": lump(11), "메모": "해외영업팀 회비"})
            elif ti == 13:
                tx.append({"거래일시": when(-5), "입금자명": mgr, "금액": lump(13), "메모": f"{code} 회비"})
                tx.append({"거래일시": when(-2), "입금자명": member(13, 6).name, "금액": ev.fee_bus, "메모": "워크숍"})
            elif ti == 22:
                half = lump(22) // 2
                tx.append({"거래일시": when(-6), "입금자명": mgr, "금액": half, "메모": f"{code} 1차"})
                tx.append({"거래일시": when(-2), "입금자명": mgr, "금액": lump(22) - half, "메모": f"{code} 2차"})
            elif ti == 25:
                tx.append({"거래일시": when(-3), "입금자명": member(25, 1).name, "금액": lump(25), "메모": f"{code} 회비"})
            else:
                tx.append({"거래일시": when(-7 + (ti % 5)), "입금자명": mgr, "금액": lump(ti), "메모": f"{code} 워크숍회비"})
        tx.append({"거래일시": when(-3), "입금자명": "ㅎㅂ", "금액": lump(16), "메모": ""})
        tx.append({"거래일시": when(-2), "입금자명": "김민준", "금액": ev.fee_bus, "메모": ""})
        tx.append({"거래일시": when(-1), "입금자명": "결산이자", "금액": 37, "메모": "이자"})
        tx.sort(key=lambda r: r["거래일시"])
        (SAMPLES_DIR / "transactions_batch1.csv").write_bytes(to_csv(tx))
        clock.freeze(kst(base, -0.2))
        payments.import_transactions(db, ev, to_csv(tx), "transactions_batch1.csv", f"admin:{admin.id}")

        # 거래내역 2차(시연 중 업로드용): T24 입금 + 1차와 같은 행 1개(중복 업로드 방지 확인)
        batch2 = [tx[0], {"거래일시": when(0), "입금자명": member(24, 0).name, "금액": lump(24), "메모": "T24 늦어서 죄송합니다"}]
        (SAMPLES_DIR / "transactions_batch2.csv").write_bytes(to_csv(batch2))

        # 정산 Agent 1회 실행(제안·확인 과제 생성)
        from .agents.settlement_agent import review_event

        clock.freeze(kst(base, -0.1))
        review = review_event(db, ev, trigger="seed")
        db.commit()
        return {"event_id": ev.id, "participants": len(rows), "teams": 25, "transactions": len(tx), "review": review["summary"]}
    finally:
        clock.freeze(None)
        db.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="가상 300명·25팀 시연 데이터 생성")
    ap.add_argument("--reset", action="store_true", help="기존 로컬 DB 를 지우고 새로 만든다")
    ap.add_argument("--base-date", help="기준일(KST, YYYY-MM-DD). 기본: 오늘")
    ap.add_argument("--seed", type=int, default=20261015)
    args = ap.parse_args()
    if not settings.is_demo:
        raise SystemExit("시드 데이터는 APP_MODE=demo 에서만 만들 수 있습니다")
    url = settings.database_url
    if args.reset and url.startswith("sqlite:///"):
        path = Path(url.removeprefix("sqlite:///"))
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(path) + suffix).unlink(missing_ok=True)
    base = date.fromisoformat(args.base_date) if args.base_date else clock.kst(clock.now()).date()
    result = seed(base, args.seed)
    print(f"시드 완료: {result}")
    print(f"샘플 CSV: {SAMPLES_DIR}")
    print(f"총무 로그인: {settings.admin_email} / (ADMIN_PASSWORD, 시연 기본값은 README 참고)")


if __name__ == "__main__":
    main()
