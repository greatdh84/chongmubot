from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str):
    if url.startswith("sqlite:///") and ":memory:" not in url:
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, connect_args={"check_same_thread": False})

    # pysqlite 의 암묵적 트랜잭션 처리를 끄고 SQLAlchemy 가 BEGIN 을 직접 내게 한다(SQLAlchemy 권장 방식).
    # SQLite 트랜잭션은 직렬화 격리라서 "배정 합계 확인 → 저장" 사이에 다른 쓰기가 끼면 한쪽이
    # 'database is locked' 로 실패한다(초과 배정이 저장되지는 않음). 중복 배정은 유니크 인덱스가 최종 방어.
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _):
        dbapi_conn.isolation_level = None
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn):
        conn.exec_driver_sql("BEGIN")

    return engine


engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=True, expire_on_commit=False)


def configure(url: str) -> None:
    """테스트·초기화 스크립트에서 DB 위치를 바꿀 때 사용."""
    global engine
    engine = make_engine(url)
    SessionLocal.configure(bind=engine)


def init_db() -> None:
    from . import models  # noqa: F401

    Base.metadata.create_all(engine)
    _add_missing_columns()


# create_all 은 기존 테이블에 새 컬럼을 넣지 않으므로, 이전 버전 DB 를 위해 필요한 컬럼만 추가한다.
_COLUMN_UPGRADES = {
    "events": {"mode": "VARCHAR(10) NOT NULL DEFAULT 'large'"},
    "change_logs": {"rule_missing": "BOOLEAN NOT NULL DEFAULT 0", "admin_refund_decided": "BOOLEAN NOT NULL DEFAULT 0"},
    "email_jobs": {"claimed_at": "DATETIME", "claimed_by": "VARCHAR(100) NOT NULL DEFAULT ''"},
}


def _add_missing_columns() -> None:
    with engine.begin() as conn:
        have = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(participants)")}
        if have and "slack_id" not in have:
            # 이메일 연락 시절의 DB: 참가자 표 구조(연락처·유일성 조건)가 달라 자동으로 고칠 수 없다
            raise RuntimeError("이전 버전(이메일) DB 입니다. 연락이 Slack 으로 바뀌어 다시 만들어야 합니다: cd large/backend && ../../.venv/bin/python -m app.seed --reset")
        for table, cols in _COLUMN_UPGRADES.items():
            have = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            for col, ddl in cols.items():
                if col not in have:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
