"""금액 계산 규칙(순수 함수)."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import fees

EV = SimpleNamespace(fee_bus=20000, fee_car=15000, fee_absent=0)
PAY_DL = datetime(2026, 10, 10)
EVENT = datetime(2026, 10, 20)
RULES = [
    SimpleNamespace(id=1, change_type="attend_to_absent", valid_until=PAY_DL, refund_percent=100, description="마감 전 100%"),
    SimpleNamespace(id=2, change_type="attend_to_absent", valid_until=EVENT - timedelta(days=3), refund_percent=50, description="3일 전까지 50%"),
    SimpleNamespace(id=3, change_type="bus_to_car", valid_until=PAY_DL, refund_percent=100, description="마감 전 차액 100%"),
    SimpleNamespace(id=4, change_type="bus_to_car", valid_until=None, refund_percent=0, description="마감 후 0%"),
]


def test_fee_for_demo_fees():
    assert fees.fee_for(EV, "bus") == 20000
    assert fees.fee_for(EV, "car") == 15000
    assert fees.fee_for(EV, "absent") == 0
    assert fees.fee_for(EV, "undecided") is None  # 미정은 회비 미확정(불참 아님)
    assert fees.fee_for(EV, None) is None  # 미응답도 불참으로 보지 않음


def test_cancel_before_payment_deadline_full_refund():
    ev = fees.evaluate_post_deadline_change(EV, RULES, "bus", "absent", 20000, PAY_DL - timedelta(days=1))
    assert (ev.refund_amount, ev.liability_after, ev.rule_id) == (20000, 0, 1)


def test_cancel_after_payment_deadline_partial_refund():
    ev = fees.evaluate_post_deadline_change(EV, RULES, "bus", "absent", 20000, PAY_DL + timedelta(days=1))
    assert (ev.refund_amount, ev.liability_after, ev.rule_id) == (10000, 10000, 2)


def test_cancel_too_late_has_no_rule_and_no_refund():
    ev = fees.evaluate_post_deadline_change(EV, RULES, "bus", "absent", 20000, EVENT - timedelta(days=1))
    assert ev.refund_amount == 0 and ev.liability_after == 20000 and ev.needs_admin_rule


def test_bus_to_car_after_payment_deadline_no_refund():
    ev = fees.evaluate_post_deadline_change(EV, RULES, "bus", "car", 20000, PAY_DL + timedelta(hours=1))
    assert ev.refund_amount == 0 and ev.liability_after == 20000 and ev.rule_id == 4


def test_bus_to_car_before_payment_deadline_refunds_difference():
    ev = fees.evaluate_post_deadline_change(EV, RULES, "bus", "car", 20000, PAY_DL - timedelta(hours=1))
    assert ev.refund_amount == 5000 and ev.liability_after == 15000


def test_upgrade_charges_difference():
    ev = fees.evaluate_post_deadline_change(EV, RULES, "car", "bus", 15000, PAY_DL)
    assert ev.extra_charge == 5000 and ev.liability_after == 20000


def test_undecided_at_snapshot_then_attend_charges_full_fee():
    ev = fees.evaluate_post_deadline_change(EV, RULES, "undecided", "bus", None, PAY_DL)
    assert ev.extra_charge == 20000 and ev.liability_after == 20000


def test_cancel_then_reattend_does_not_double_charge():
    first = fees.evaluate_post_deadline_change(EV, RULES, "bus", "absent", 20000, PAY_DL + timedelta(days=1))
    again = fees.evaluate_post_deadline_change(EV, RULES, "absent", "bus", first.liability_after, PAY_DL + timedelta(days=2))
    assert first.liability_after == 10000 and again.liability_after == 20000 and again.extra_charge == 10000


def test_undecided_not_allowed_after_deadline():
    with pytest.raises(ValueError):
        fees.evaluate_post_deadline_change(EV, RULES, "bus", "undecided", 20000, PAY_DL)


@pytest.mark.parametrize(
    "final_fee,received,paid,balance,label",
    [
        (240000, 200000, 0, 40000, "추가 납부"),
        (220000, 240000, 0, -20000, "추가 환불"),
        (220000, 240000, 20000, 0, "정산 완료"),  # 환불 송금 후 잔액 0
        (220000, 240000, 5000, -15000, "추가 환불"),
    ],
)
def test_settlement_balance_formula(final_fee, received, paid, balance, label):
    assert fees.settlement_balance(final_fee, received, paid) == balance
    assert fees.balance_label(balance) == label
