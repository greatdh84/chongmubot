"""입금 배정(초과·중복 방지), 거래내역 중복 업로드, 정산 계산."""
from datetime import timedelta

import pytest

from app import clock
from app.agents.settlement_agent import review_event
from app.errors import AppError
from app.models import Allocation, ConfirmTask
from app.services import payments, responses, settlement

from .conftest import NOW, make_world

CSV_HEADER = "거래일시,입금자명,금액,메모\n"


def upload(db, w, rows: str, name="t.csv"):
    r = payments.import_transactions(db, w.event, (CSV_HEADER + rows).encode(), name, "admin:1")
    db.commit()
    return r


def all_bus(db, w):
    for ps in w.members.values():
        for p in ps:
            responses.submit_change(db, w.event, p, "bus", source="web", actor="t")
    db.commit()


def test_allocation_sum_cannot_exceed_transaction(db):
    w = make_world(db)
    upload(db, w, "2026-10-07 10:00,누군가,50000,\n")
    txn_id = db.query(payments.BankTransaction).one().id
    payments.allocate(db, w.event, txn_id, w.teams[0].id, 30000, source="admin", basis="", actor="t")
    payments.allocate(db, w.event, txn_id, w.teams[1].id, 20000, source="admin", basis="", actor="t")
    with pytest.raises(AppError) as e:
        payments.allocate(db, w.event, txn_id, w.teams[2].id, 1, source="admin", basis="", actor="t")
    assert e.value.code == "ALLOCATION_EXCEEDS_TRANSACTION"


def test_duplicate_allocation_rejected_until_cancelled(db):
    w = make_world(db)
    upload(db, w, "2026-10-07 10:00,누군가,50000,\n")
    txn_id = db.query(payments.BankTransaction).one().id
    a = payments.allocate(db, w.event, txn_id, w.teams[0].id, 10000, source="admin", basis="", actor="t")
    with pytest.raises(AppError) as e:
        payments.allocate(db, w.event, txn_id, w.teams[0].id, 10000, source="admin", basis="", actor="t")
    assert e.value.code == "DUPLICATE_ALLOCATION"
    payments.cancel_allocation(db, w.event, a.id, "t")
    payments.allocate(db, w.event, txn_id, w.teams[0].id, 50000, source="admin", basis="", actor="t")  # 취소분은 합계에서 빠짐
    db.commit()
    assert payments.allocated_sum(db, txn_id) == 50000


def test_db_index_blocks_duplicate_even_if_service_check_skipped(db):
    w = make_world(db)
    upload(db, w, "2026-10-07 10:00,누군가,50000,\n")
    txn_id = db.query(payments.BankTransaction).one().id
    db.add(Allocation(event_id=w.event.id, transaction_id=txn_id, team_id=w.teams[0].id, amount=1, status="proposed", source="t"))
    db.commit()
    db.add(Allocation(event_id=w.event.id, transaction_id=txn_id, team_id=w.teams[0].id, amount=1, status="proposed", source="t"))
    with pytest.raises(Exception):
        db.commit()


def test_personal_allocation_must_match_team_and_event(db):
    w = make_world(db)
    other = make_world(db, email="other@example.com", prefix="O")
    upload(db, w, "2026-10-07 10:00,누군가,20000,\n")
    txn_id = db.query(payments.BankTransaction).filter_by(event_id=w.event.id).one().id
    with pytest.raises(AppError) as e:
        payments.allocate(db, w.event, txn_id, w.teams[0].id, 20000, participant_id=w.members[1][1].id, source="admin", basis="", actor="t")
    assert e.value.code == "PARTICIPANT_TEAM_MISMATCH"
    with pytest.raises(AppError) as e:
        payments.allocate(db, other.event, txn_id, other.teams[0].id, 20000, source="admin", basis="", actor="t")
    assert e.value.status == 404  # 다른 행사의 거래


def test_reupload_same_csv_does_not_duplicate_but_identical_rows_in_file_kept(db):
    w = make_world(db)
    rows = "2026-10-07 10:00,홍길동,20000,\n2026-10-07 10:00,홍길동,20000,\n"
    assert upload(db, w, rows)["inserted"] == 2  # 같은 파일 안 동일 행은 실제 2건일 수 있음
    again = upload(db, w, rows)
    assert again["inserted"] == 0 and again["duplicates"] == 2


def test_individual_deposit_counts_toward_team_and_team_fee_unchanged(db):
    w = make_world(db, size=3)
    all_bus(db, w)
    team, (m0, m1, m2) = w.teams[0], w.members[0]
    upload(db, w, f"2026-10-07 10:00,{m0.name},40000,T01\n2026-10-07 11:00,{m2.name},20000,\n")
    t1, t2 = db.query(payments.BankTransaction).order_by(payments.BankTransaction.id).all()
    payments.allocate(db, w.event, t1.id, team.id, 40000, status="confirmed", source="admin", basis="", actor="t")
    payments.allocate(db, w.event, t2.id, team.id, 20000, participant_id=m2.id, status="confirmed", source="admin", basis="", actor="t")
    calc = settlement.compute_team(db, w.event, team)
    assert calc["final_fee"] == 60000  # 팀 총 회비는 그대로
    assert calc["received"] == 60000  # 개인 입금도 팀 수납액에 포함
    assert calc["balance"] == 0


def test_only_confirmed_allocations_count_as_received(db):
    w = make_world(db, size=2)
    all_bus(db, w)
    upload(db, w, f"2026-10-07 10:00,{w.members[0][0].name},40000,T01\n")
    txn = db.query(payments.BankTransaction).one()
    payments.allocate(db, w.event, txn.id, w.teams[0].id, 40000, status="proposed", source="agent", basis="", actor="t")
    calc = settlement.compute_team(db, w.event, w.teams[0])
    assert calc["received"] == 0 and calc["balance"] == 40000
    assert any("배정 제안" in b for b in calc["blockers"])


def test_refund_flow_balance_and_payout(db):
    w = make_world(db, size=2)
    all_bus(db, w)
    team = w.teams[0]
    clock.freeze(NOW + timedelta(days=3))  # 참석 마감 후
    ch = responses.submit_change(db, w.event, w.members[0][1], "absent", source="web", actor="t")
    assert ch.status == "pending_approval"
    responses.decide_change(db, w.event, ch, w.admin, approve=True)
    upload(db, w, f"2026-10-07 10:00,{w.members[0][0].name},40000,T01\n")
    txn = db.query(payments.BankTransaction).one()
    payments.allocate(db, w.event, txn.id, team.id, 40000, status="confirmed", source="admin", basis="", actor="t")
    db.commit()
    calc = settlement.compute_team(db, w.event, team)
    # 요청 시각이 입금 마감 시각과 같으면 '마감 전 100%'가 아니라 다음 규칙(50%)이 적용된다
    assert calc["final_fee"] == 20000 + 10000
    assert calc["balance"] == -10000
    settlement.confirm(db, w.event, team.id, w.admin)
    with pytest.raises(AppError):
        settlement.record_payout(db, w.event, team.id, 10001, "", w.admin)  # 남은 환불액 초과 불가
    settlement.record_payout(db, w.event, team.id, 10000, "송금 완료", w.admin)
    assert settlement.compute_team(db, w.event, team)["balance"] == 0


def test_unanswered_is_not_absent_and_blocks_confirmation(db):
    w = make_world(db, size=2, after_attend=True)
    p0, p1 = w.members[0]
    clock.freeze(NOW - timedelta(days=5))
    responses.submit_change(db, w.event, p0, "bus", source="web", actor="t")  # p1 은 미응답
    clock.freeze(NOW)
    responses.ensure_snapshot(db, w.event)
    db.commit()
    calc = settlement.compute_team(db, w.event, w.teams[0])
    line = next(l for l in calc["lines"] if l["participant_id"] == p1.id)
    assert line["fee"] is None and line["unresolved"] and line["choice"] is None
    with pytest.raises(AppError) as e:
        settlement.confirm(db, w.event, w.teams[0].id, w.admin)
    assert e.value.code == "SETTLEMENT_BLOCKED"


def test_snapshot_is_preserved_and_later_changes_are_history(db):
    w = make_world(db, size=1, after_attend=True)
    p = w.members[0][0]
    clock.freeze(NOW - timedelta(days=5))
    responses.submit_change(db, w.event, p, "bus", source="web", actor="t")
    clock.freeze(NOW)
    assert responses.ensure_snapshot(db, w.event) is True
    assert responses.ensure_snapshot(db, w.event) is False  # 다시 저장하지 않음
    ch = responses.submit_change(db, w.event, p, "car", source="web", actor="t")
    responses.decide_change(db, w.event, ch, w.admin, approve=True)
    db.commit()
    st = responses.fee_table(db, w.event)[p.id]
    assert st.snapshot_choice == "bus" and st.choice == "car"


def test_amount_match_alone_never_allocates(db):
    w = make_world(db, n_teams=2, size=2)
    all_bus(db, w)
    upload(db, w, "2026-10-07 10:00,ㅇㅇ,40000,\n")  # 두 팀 미납액과 금액이 같음, 단서 없음
    result = review_event(db, w.event)
    db.commit()
    assert db.query(Allocation).count() == 0
    tasks = db.query(ConfirmTask).filter_by(kind="unknown_depositor").all()
    assert {t.team_id for t in tasks} == {w.teams[0].id, w.teams[1].id}
    assert result["counts"].get("proposed", 0) == 0


def test_team_code_in_memo_proposes_but_does_not_confirm(db):
    w = make_world(db, size=2)
    all_bus(db, w)
    upload(db, w, f"2026-10-07 10:00,{w.members[0][0].name},40000,T01 회비\n")
    review_event(db, w.event)
    db.commit()
    a = db.query(Allocation).one()
    assert a.status == "proposed" and a.confidence == "high" and a.team_id == w.teams[0].id


def test_review_is_idempotent(db):
    w = make_world(db, n_teams=2, size=2)
    all_bus(db, w)
    upload(db, w, "2026-10-07 10:00,ㅇㅇ,40000,\n")
    review_event(db, w.event)
    review_event(db, w.event)
    db.commit()
    assert db.query(ConfirmTask).filter_by(kind="unknown_depositor").count() == 2
