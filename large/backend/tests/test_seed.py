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
