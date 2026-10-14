"""명단 업로드·실시간 DM 시연용 '빈 행사' DB 만들기. 시드(가짜 300명)를 넣지 않고 행사 1개만 만든다.

    DATABASE_URL=sqlite:///$PWD/var/live.db ../../.venv/bin/python scripts/make_empty_event.py

- 시연 날짜 고정(KST): 참석 응답 마감 2026-10-05 18:00 · 회비 납부 마감 2026-10-08 18:00 · 행사 2026-10-14 09:00
  (시연용 응답·변경·거래내역 CSV 의 날짜와 맞춤. 초기설정 화면의 서버 없을 때 기본값도 같다)
- 회비 버스 20,000 / 자차 15,000 / 불참 0, 환불 규칙 4개(납부 마감 전 불참 100% · 납부 마감~행사 3일 전 불참 50%
  · 버스→자차 납부 마감 전 100% · 이후 0%). 명단은 초기설정 화면(워크샵)에서 CSV 로 올린다.
- 이미 행사가 있는 DB 면 거부한다(시드 DB·운영 DB 보호). 지우고 다시 만들려면 large/scripts/start_live.sh --reset
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import func, select  # noqa: E402

from app import clock, db as dbmod  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import Admin, DbMeta, Event, RefundRule  # noqa: E402
from app.security import hash_password  # noqa: E402

LIVE_PURPOSE = "demo-live"


# 시연 날짜(KST)
ATTEND_DEADLINE = datetime(2026, 10, 5, 18, 0)
PAYMENT_DEADLINE = datetime(2026, 10, 8, 18, 0)
EVENT_AT = datetime(2026, 10, 14, 9, 0)


def main() -> None:
    if not settings.is_demo:
        raise SystemExit("빈 행사 만들기는 APP_MODE=demo 에서만 합니다")
    dbmod.init_db()
    db = dbmod.SessionLocal()
    try:
        n = db.scalar(select(func.count()).select_from(Event))
        if n:
            raise SystemExit(f"이미 행사가 {n}개 있는 DB 입니다({settings.database_url}). 지우고 새로 만들려면 large/scripts/start_live.sh --reset")
        admin = db.scalar(select(Admin).where(Admin.email == settings.admin_email.lower()))
        if admin is None:
            admin = Admin(email=settings.admin_email.lower(), password_hash=hash_password(settings.admin_password))
            db.add(admin)
            db.flush()
        attend, payment, event_at = (clock.kst_naive_to_utc(d) for d in (ATTEND_DEADLINE, PAYMENT_DEADLINE, EVENT_AT))
        ev = Event(
            admin_id=admin.id, name="2026 가을 전사 워크숍", event_at=event_at, attend_deadline=attend, payment_deadline=payment,
            mode="large", fee_bus=20000, fee_car=15000, fee_absent=0, account_note="(시연) 가상은행 000-000000-00 워크숍 총무",
        )
        db.add(ev)
        db.flush()
        for ctype, until, pct, desc in [
            ("attend_to_absent", payment, 100, "입금 마감 전 불참 전환: 전액 환불"),
            ("attend_to_absent", event_at - timedelta(days=3), 50, "입금 마감 후 ~ 행사 3일 전 불참: 50% 환불"),
            ("bus_to_car", payment, 100, "입금 마감 전 버스→자차: 차액 전액 환불"),
            ("bus_to_car", None, 0, "입금 마감 후 버스→자차: 버스 좌석 확정으로 환불 없음"),
        ]:
            db.add(RefundRule(event_id=ev.id, change_type=ctype, valid_until=until, refund_percent=pct, description=desc))
        db.merge(DbMeta(key="purpose", value=LIVE_PURPOSE))
        db.commit()
        print(f"빈 행사 생성: #{ev.id} {ev.name} · 참석 마감 {clock.fmt(attend)} · 납부 마감 {clock.fmt(payment)} · 행사 {clock.fmt(event_at)} ({settings.database_url})")
    finally:
        db.close()


if __name__ == "__main__":
    main()
