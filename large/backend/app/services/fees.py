"""회비·환불·정산 계산(순수 함수). 금액 계산은 LLM 이 아니라 여기서만 한다.

규칙 요약
- 회비: 버스 fee_bus, 자차 fee_car, 불참 fee_absent. 미정·미응답은 회비 미확정(None).
- 참석 마감 전 변경: 현재 선택의 회비가 곧 부담액.
- 참석 마감 후 변경(부담액 liability 기준):
  * 부담액이 늘어나는 변경(자차→버스, 불참→참석 등): liability = max(이전 부담액, 새 회비). 추가분은 전액 청구.
  * 줄어드는 변경(참석→불참, 버스→자차): 환불 대상 = 이전 부담액 - 새 회비,
    환불액 = 환불 대상 × 규칙의 환불률. 맞는 규칙이 없으면 환불 0(총무 확인).
- 정산 잔액 = 최종 회비 - 확정 수납액 + 이미 지급한 환불액 (양수: 추가 납부, 음수: 추가 환불)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional, Protocol


class FeeConfig(Protocol):
    fee_bus: int
    fee_car: int
    fee_absent: int


class RuleLike(Protocol):
    id: int
    change_type: str
    valid_until: Optional[datetime]
    refund_percent: int
    description: str


def fee_for(event: FeeConfig, choice: Optional[str]) -> Optional[int]:
    if choice == "bus":
        return event.fee_bus
    if choice == "car":
        return event.fee_car
    if choice == "absent":
        return event.fee_absent
    return None  # undecided / 미응답


def change_type(from_choice: Optional[str], to_choice: str) -> Optional[str]:
    """환불 규칙을 찾을 때 쓰는 변경 유형. 환불과 무관한 변경은 None."""
    if from_choice in ("bus", "car") and to_choice == "absent":
        return "attend_to_absent"
    if from_choice == "bus" and to_choice == "car":
        return "bus_to_car"
    return None


def match_rule(rules: Iterable[RuleLike], ctype: str, requested_at: datetime) -> Optional[RuleLike]:
    candidates = [r for r in rules if r.change_type == ctype and (r.valid_until is None or requested_at < r.valid_until)]
    candidates.sort(key=lambda r: (r.valid_until is None, r.valid_until or datetime.max))
    return candidates[0] if candidates else None


@dataclass
class ChangeEval:
    liability_before: int
    liability_after: int
    refund_amount: int
    extra_charge: int
    rule_id: Optional[int]
    rule_text: str
    needs_admin_rule: bool  # 감액 변경인데 맞는 환불 규칙이 없음


def evaluate_post_deadline_change(
    event: FeeConfig,
    rules: Iterable[RuleLike],
    from_choice: Optional[str],
    to_choice: str,
    liability_before: Optional[int],
    requested_at: datetime,
) -> ChangeEval:
    if to_choice not in ("bus", "car", "absent"):
        raise ValueError("마감 후에는 미정으로 변경할 수 없습니다")
    before = liability_before or 0
    new_fee = fee_for(event, to_choice) or 0
    if new_fee >= before:
        after = new_fee
        return ChangeEval(before, after, 0, after - before, None, "추가 회비는 전액 청구", False)
    ctype = change_type(from_choice, to_choice)
    rule = match_rule(rules, ctype, requested_at) if ctype else None
    refundable = before - new_fee
    if rule is None:
        return ChangeEval(before, before, 0, 0, None, "적용할 환불 규칙 없음 → 환불 0원, 총무 확인 필요", True)
    refund = refundable * rule.refund_percent // 100
    text = rule.description or f"{ctype} 환불 {rule.refund_percent}%"
    return ChangeEval(before, before - refund, refund, 0, rule.id, text, False)


def settlement_balance(final_fee: int, received: int, refunds_paid: int) -> int:
    """정산 잔액 = 최종 회비 - 확정 수납액 + 이미 지급한 환불액."""
    return final_fee - received + refunds_paid


def balance_label(balance: int) -> str:
    if balance > 0:
        return "추가 납부"
    if balance < 0:
        return "추가 환불"
    return "정산 완료"
