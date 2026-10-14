from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Choice = Literal["bus", "car", "absent", "undecided"]


class AdminLogin(BaseModel):
    email: str
    password: str


class InviteExchange(BaseModel):
    token: str = Field(min_length=10, max_length=200)


class ManagerCodeRequest(BaseModel):
    login_id: str = Field(min_length=2, max_length=40)  # 명단의 ID 또는 Slack ID


class ManagerCodeVerify(BaseModel):
    login_id: str = Field(min_length=2, max_length=40)
    code: str = Field(min_length=6, max_length=6)


class ResponseSubmit(BaseModel):
    choice: Choice
    note: str = ""


class ChatSend(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class EventUpdate(BaseModel):
    mode: Optional[str] = None  # 받기만 하고 바꾸지 않음(모드 변경 금지 검사용)
    name: Optional[str] = None
    event_at: Optional[str] = None
    attend_deadline: Optional[str] = None
    payment_deadline: Optional[str] = None
    fee_bus: Optional[int] = Field(default=None, ge=0)
    fee_car: Optional[int] = Field(default=None, ge=0)
    fee_absent: Optional[int] = Field(default=None, ge=0)
    account_note: Optional[str] = None


class RefundRuleIn(BaseModel):
    change_type: Literal["attend_to_absent", "bus_to_car"]
    valid_until: Optional[str] = None
    refund_percent: int = Field(ge=0, le=100)
    description: str = ""


class ManagerAssign(BaseModel):
    participant_id: int


class AdminResponseSet(BaseModel):
    choice: Choice
    note: str = ""


class Decision(BaseModel):
    note: str = Field(default="", max_length=300)
    # 환불 규칙이 없는 변경을 승인할 때만: 총무가 정한 환불액
    refund_amount: Optional[int] = Field(default=None, ge=0)


class AllocationIn(BaseModel):
    team_id: int
    participant_id: Optional[int] = None
    amount: int
    basis: str = Field(default="총무 수동 배정", max_length=500)
    confirm: bool = True


class TaskAnswer(BaseModel):
    option: str
    note: str = Field(default="", max_length=500)


class TaskResolve(BaseModel):
    resolution: str = Field(min_length=1, max_length=500)


class PayoutIn(BaseModel):
    amount: int
    memo: str = Field(default="", max_length=300)


class EmailEnqueue(BaseModel):
    kind: Literal["invite", "attend_reminder", "payment_reminder", "refund_notice", "extra_notice"]
    team_ids: Optional[list[int]] = None  # 환불·추가 납부 안내를 이 부서들에게만(대시보드 부서 상세)


class RefundAccountIn(BaseModel):
    bank: str = Field(max_length=30)
    account_no: str = Field(max_length=40)
    holder: str = Field(max_length=40)


class RefundLinkIn(BaseModel):
    token: str = Field(max_length=200)


class RefundLinkSubmit(RefundAccountIn):
    token: str = Field(max_length=200)


class RevealIn(BaseModel):
    purpose: Literal["view", "copy"] = "view"


class SchedulerToggle(BaseModel):
    enabled: bool


class CorrectionIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)


# 소모임(demo/index.html)의 PERSONAS·STRATEGIES 키와 같아야 한다
BOT_PERSONAS = ("active", "shy", "oldie", "strict")
BOT_STRATEGIES = ("consistent", "escalate", "chaos")


class BotSettings(BaseModel):
    persona: Literal["active", "shy", "oldie", "strict"]
    strategy: Literal["consistent", "escalate", "chaos"]


class DevSwitch(BaseModel):
    role: Literal["admin", "participant", "manager"]
    participant_id: Optional[int] = None
