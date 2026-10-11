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
    email: str


class ManagerCodeVerify(BaseModel):
    email: str
    code: str = Field(min_length=6, max_length=6)


class ResponseSubmit(BaseModel):
    choice: Choice
    note: str = ""


class ChatSend(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class EventUpdate(BaseModel):
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
    note: str = ""


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
    kind: Literal["invite", "attend_reminder", "payment_reminder"]


class CorrectionIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class DevSwitch(BaseModel):
    role: Literal["admin", "participant", "manager"]
    participant_id: Optional[int] = None
