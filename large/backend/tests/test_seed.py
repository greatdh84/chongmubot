"""시드 데이터: 300명·25팀, 같은 시드·기준일이면 같은 결과."""
from datetime import date

from sqlalchemy import func, select

from app import db as dbmod, seed as seedmod
from app.models import BankTransaction, ConfirmTask, Participant, Team, TeamManager


def _fingerprint():
    with dbmod.SessionLocal() as s:
        people = s.execute(select(Participant.employee_no, Participant.name, Participant.email).order_by(Participant.employee_no)).all()
        txns = s.execute(select(BankTransaction.depositor, BankTransaction.amount).order_by(BankTransaction.id)).all()
        kinds = dict(s.execute(select(ConfirmTask.kind, func.count()).group_by(ConfirmTask.kind)).all())
        return people, txns, kinds, s.scalar(select(func.count()).select_from(Team)), s.scalar(select(func.count()).select_from(TeamManager))


def test_seed_shape_and_reproducible(tmp_path, monkeypatch):
    monkeypatch.setattr(seedmod, "SAMPLES_DIR", tmp_path / "samples")
    seedmod.seed(date(2026, 10, 8))
    first = _fingerprint()
    people, txns, kinds, teams, managers = first
    assert len(people) == 300 and teams == 25 and managers == 24  # T25 담당자 미지정
    assert sum(1 for p in people if p[1] == "김민준") == 2  # 동명이인
    assert {"ambiguous_depositor", "unknown_depositor", "possible_duplicate", "change_approval"} <= set(kinds)

    dbmod.configure(f"sqlite:///{tmp_path / 'second.db'}")
    seedmod.seed(date(2026, 10, 8))
    assert _fingerprint() == first


def test_reset_only_touches_demo_seeded_db(tmp_path, monkeypatch):
    import sqlite3

    import pytest

    from app.config import settings

    # 시드 표시가 없는 DB(운영 데이터로 간주) → 거부
    prod = tmp_path / "prod.db"
    con = sqlite3.connect(prod)
    con.execute("CREATE TABLE events (id INTEGER)")
    con.execute("INSERT INTO events VALUES (1)")
    con.commit()
    con.close()
    assert "표시" in seedmod.check_resettable(prod)
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{prod}")
    monkeypatch.setattr("sys.argv", ["seed", "--reset"])
    with pytest.raises(SystemExit) as e:
        seedmod.main()
    assert "초기화 거부" in str(e.value) and prod.exists()

    # 운영 모드 → 거부
    monkeypatch.setattr(settings, "app_mode", "production")
    with pytest.raises(SystemExit) as e:
        seedmod.main()
    assert "운영 모드" in str(e.value)


def test_seeded_db_is_marked_and_resettable(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(seedmod, "SAMPLES_DIR", tmp_path / "samples")
    path = tmp_path / "demo.db"
    dbmod.configure(f"sqlite:///{path}")
    result = seedmod.seed(date(2026, 10, 15))
    assert seedmod.check_resettable(path) is None
    assert [c["team"] for c in result["cast"]][:2] == ["T10", "T10"]
    # 같은 조건이면 등장인물도 같다
    dbmod.configure(f"sqlite:///{tmp_path / 'again.db'}")
    assert seedmod.seed(date(2026, 10, 15))["cast"] == result["cast"]
