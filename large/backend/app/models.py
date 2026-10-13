"""공통 데이터 모델. 모든 연결은 이름이 아니라 ID(FK)로 한다.

시각 컬럼은 전부 naive UTC. 금액은 원 단위 정수.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from . import clock
from .db import Base

CHOICES = ("bus", "car", "absent", "undecided")
CHOICE_LABELS = {"bus": "참석+버스", "car": "참석+자차", "absent": "불참", "undecided": "미정", None: "미응답"}


def _now() -> datetime:
    return clock.now()


class Admin(Base):
    __tablename__ = "admins"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(default=_now)


class Event(Base):
    """행사. 참석 마감과 팀 입금 마감을 분리한다."""

    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    admin_id: Mapped[int] = mapped_column(ForeignKey("admins.id"))
    name: Mapped[str] = mapped_column(String(200))
    event_at: Mapped[datetime]
    attend_deadline: Mapped[datetime]
    payment_deadline: Mapped[datetime]
    fee_bus: Mapped[int] = mapped_column(default=20000)
    fee_car: Mapped[int] = mapped_column(default=15000)
    fee_absent: Mapped[int] = mapped_column(default=0)
    account_note: Mapped[str] = mapped_column(String(300), default="")
    # 총무봇 공용 서버에서 SMALL/LARGE 를 구분. 이 DB 는 LARGE 전용이며 만든 뒤에는 바꾸지 않는다.
    mode: Mapped[str] = mapped_column(String(10), default="large", server_default="large")
    snapshot_taken_at: Mapped[Optional[datetime]] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class Team(Base):
    """팀 = 부서. 명단 CSV 의 '소속 부서' 하나가 팀 하나이고, 담당자(팀장)가 팀 회비를 한 번에 걷어 낸다."""

    __tablename__ = "teams"
    __table_args__ = (UniqueConstraint("event_id", "code"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    code: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(100))


class Participant(Base):
    __tablename__ = "participants"
    """참가자. 연락은 Slack DM(slack_id)으로 한다. employee_no 는 명단 CSV 의 ID 열."""

    __table_args__ = (UniqueConstraint("event_id", "slack_id"), UniqueConstraint("event_id", "employee_no"))
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    employee_no: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(50))
    slack_id: Mapped[str] = mapped_column(String(40))
    phone: Mapped[str] = mapped_column(String(30), default="")


class TeamManager(Base):
    """팀 담당자 지정. 총무만 지정할 수 있고, 팀당 활성 담당자는 1명."""

    __tablename__ = "team_managers"
    __table_args__ = (
        Index("uq_active_manager_per_team", "team_id", unique=True, sqlite_where=text("revoked_at IS NULL")),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"))
    assigned_by: Mapped[int] = mapped_column(ForeignKey("admins.id"))
    assigned_at: Mapped[datetime] = mapped_column(default=_now)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(default=None)


class Invite(Base):
    """개인별 초대 토큰. 원문은 저장하지 않고 SHA-256 해시만 저장."""

    __tablename__ = "invites"
    id: Mapped[int] = mapped_column(primary_key=True)
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime]
    revoked_at: Mapped[Optional[datetime]] = mapped_column(default=None)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class AuthSession(Base):
    """로그인 세션. role 별로 다른 쿠키를 쓴다(admin / participant / manager)."""

    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    role: Mapped[str] = mapped_column(String(20))
    event_id: Mapped[Optional[int]] = mapped_column(ForeignKey("events.id"))
    admin_id: Mapped[Optional[int]] = mapped_column(ForeignKey("admins.id"))
    participant_id: Mapped[Optional[int]] = mapped_column(ForeignKey("participants.id"))
    team_id: Mapped[Optional[int]] = mapped_column(ForeignKey("teams.id"))
    via: Mapped[str] = mapped_column(String(30), default="")  # password | invite | slack_code | demo_switch
    expires_at: Mapped[datetime]
    revoked_at: Mapped[Optional[datetime]] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class ManagerCode(Base):
    """담당자 이메일 인증 코드(해시 저장, 10분 유효, 5회 시도 제한)."""

    __tablename__ = "manager_codes"
    id: Mapped[int] = mapped_column(primary_key=True)
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    code_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime]
    attempts: Mapped[int] = mapped_column(default=0)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class Response(Base):
    """현재 응답(참가자당 1행). 행이 없으면 미응답."""

    __tablename__ = "responses"
    id: Mapped[int] = mapped_column(primary_key=True)
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"), unique=True)
    choice: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(20), default="web")
    updated_at: Mapped[datetime] = mapped_column(default=_now)


class DeadlineSnapshot(Base):
    """참석 마감 기준 응답. 한 번 저장하면 수정하지 않는다(이후 변경은 ChangeLog 로만 추가)."""

    __tablename__ = "deadline_snapshots"
    __table_args__ = (UniqueConstraint("event_id", "participant_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"))
    choice: Mapped[Optional[str]] = mapped_column(String(20))  # None = 미응답
    fee: Mapped[Optional[int]]  # 미응답·미정이면 None(회비 미확정)
    captured_at: Mapped[datetime] = mapped_column(default=_now)


class RefundRule(Base):
    """변경 시점·항목별 환불 규칙.
    change_type: attend_to_absent(참석→불참) | bus_to_car(버스→자차)
    valid_until 이전에 요청된 변경에 refund_percent 를 적용. 여러 개면 valid_until 이 가장 이른 것부터 본다.
    """

    __tablename__ = "refund_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    change_type: Mapped[str] = mapped_column(String(30))
    valid_until: Mapped[Optional[datetime]]  # None = 기한 없음
    refund_percent: Mapped[int]
    description: Mapped[str] = mapped_column(String(300), default="")


class ChangeLog(Base):
    """응답 변경 이력. 마감 후 변경은 총무 승인(pending_approval → applied/rejected)을 거친다."""

    __tablename__ = "change_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"), index=True)
    from_choice: Mapped[Optional[str]] = mapped_column(String(20))
    to_choice: Mapped[str] = mapped_column(String(20))
    after_deadline: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20))  # applied | pending_approval | rejected
    source: Mapped[str] = mapped_column(String(20))  # web | chat | admin | seed
    requested_at: Mapped[datetime] = mapped_column(default=_now)
    decided_at: Mapped[Optional[datetime]] = mapped_column(default=None)
    decided_by: Mapped[Optional[int]] = mapped_column(ForeignKey("admins.id"))
    liability_before: Mapped[Optional[int]]
    liability_after: Mapped[Optional[int]]
    refund_amount: Mapped[int] = mapped_column(default=0)
    extra_charge: Mapped[int] = mapped_column(default=0)
    refund_rule_id: Mapped[Optional[int]] = mapped_column(ForeignKey("refund_rules.id"))
    rule_text: Mapped[str] = mapped_column(String(300), default="")
    # 요청 시점에 맞는 환불 규칙이 없었음 → 환불액은 '임시 0원'일 뿐 확정이 아니다(총무 결정 필요)
    rule_missing: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    admin_refund_decided: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    evidence_message_id: Mapped[Optional[int]] = mapped_column(ForeignKey("chat_messages.id"))
    note: Mapped[str] = mapped_column(String(300), default="")


class Upload(Base):
    __tablename__ = "uploads"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    kind: Mapped[str] = mapped_column(String(20))  # roster | transactions
    filename: Mapped[str] = mapped_column(String(200))
    sha256: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(default=0)
    inserted: Mapped[int] = mapped_column(default=0)
    skipped: Mapped[int] = mapped_column(default=0)
    uploaded_at: Mapped[datetime] = mapped_column(default=_now)


class BankTransaction(Base):
    """원본 입금(거래내역 CSV 한 행). 업로드 후 수정하지 않는다."""

    __tablename__ = "bank_transactions"
    __table_args__ = (UniqueConstraint("event_id", "row_hash"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    upload_id: Mapped[int] = mapped_column(ForeignKey("uploads.id"))
    txn_at: Mapped[datetime]
    depositor: Mapped[str] = mapped_column(String(100))
    amount: Mapped[int]
    memo: Mapped[str] = mapped_column(String(200), default="")
    row_hash: Mapped[str] = mapped_column(String(64))


class Allocation(Base):
    """입금 배정. 한 거래를 여러 팀/개인에게 나눌 수 있지만 합계는 거래 금액을 넘지 못한다.
    status: proposed(Agent 제안) → confirmed(총무 확정) | cancelled. 정산에는 confirmed 만 반영."""

    __tablename__ = "allocations"
    __table_args__ = (
        Index(
            "uq_active_allocation",
            "transaction_id",
            "team_id",
            text("coalesce(participant_id, 0)"),
            unique=True,
            sqlite_where=text("status != 'cancelled'"),
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("bank_transactions.id"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    participant_id: Mapped[Optional[int]] = mapped_column(ForeignKey("participants.id"))
    amount: Mapped[int]
    status: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(20))  # agent | admin
    confidence: Mapped[str] = mapped_column(String(20), default="")
    basis: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=_now)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(default=None)
    confirmed_by: Mapped[Optional[int]] = mapped_column(ForeignKey("admins.id"))
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(default=None)


class ConfirmTask(Base):
    """확인 과제. Agent 가 만들고 담당자·참가자·총무가 답한다. dedupe_key 로 같은 과제 중복 생성 방지."""

    __tablename__ = "confirm_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    assignee: Mapped[str] = mapped_column(String(20))  # manager | admin | participant
    team_id: Mapped[Optional[int]] = mapped_column(ForeignKey("teams.id"), index=True)
    participant_id: Mapped[Optional[int]] = mapped_column(ForeignKey("participants.id"))
    transaction_id: Mapped[Optional[int]] = mapped_column(ForeignKey("bank_transactions.id"))
    change_id: Mapped[Optional[int]] = mapped_column(ForeignKey("change_logs.id"))
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | answered | resolved | escalated | cancelled
    blocking: Mapped[bool] = mapped_column(Boolean, default=True)  # 열린 동안 해당 팀 정산 확정 불가
    question: Mapped[str] = mapped_column(Text)
    options: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)
    answer: Mapped[Optional[str]] = mapped_column(String(40))
    answer_note: Mapped[str] = mapped_column(Text, default="")
    answered_by: Mapped[Optional[int]] = mapped_column(ForeignKey("participants.id"))
    answered_at: Mapped[Optional[datetime]] = mapped_column(default=None)
    answer_message_id: Mapped[Optional[int]] = mapped_column(ForeignKey("chat_messages.id"))
    resolution: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    rule: Mapped[str] = mapped_column(String(300), default="")
    created_by: Mapped[str] = mapped_column(String(40))
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now)


class ChatThread(Base):
    """대화 스레드. state/pending 을 저장해 답변 대기 중인 대화를 나중에 이어간다."""

    __tablename__ = "chat_threads"
    __table_args__ = (UniqueConstraint("participant_id", "role"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"))
    role: Mapped[str] = mapped_column(String(20))  # participant | manager
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    state: Mapped[str] = mapped_column(String(30), default="idle")  # idle | awaiting_info | awaiting_confirmation
    pending: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, default=None)
    updated_at: Mapped[datetime] = mapped_column(default=_now)


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey("chat_threads.id"), index=True)
    sender: Mapped[str] = mapped_column(String(20))  # user | agent | system
    text: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class AgentRun(Base):
    """Agent 실행 기록: 근거 메시지, 호출한 도구와 결과, 사용한 규칙."""

    __tablename__ = "agent_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    agent: Mapped[str] = mapped_column(String(30))  # communication | settlement
    trigger: Mapped[str] = mapped_column(String(100))
    thread_id: Mapped[Optional[int]] = mapped_column(ForeignKey("chat_threads.id"))
    evidence_message_id: Mapped[Optional[int]] = mapped_column(ForeignKey("chat_messages.id"))
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    rules: Mapped[list[str]] = mapped_column(JSON, default=list)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    llm_mode: Mapped[str] = mapped_column(String(20), default="mock")
    created_at: Mapped[datetime] = mapped_column(default=_now)


class EmailJob(Base):
    """알림 발송 작업(Slack DM). 테이블 이름은 이전 버전 호환을 위해 그대로 둔다.
    to_email 에는 받는 사람의 Slack ID 가 들어간다. 본문에 토큰을 저장하지 않고 발송 직전에 렌더링한다."""

    __tablename__ = "email_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))  # invite | attend_reminder | payment_reminder | confirm_request | manager_code | refund_notice | extra_notice
    participant_id: Mapped[Optional[int]] = mapped_column(ForeignKey("participants.id"))
    team_id: Mapped[Optional[int]] = mapped_column(ForeignKey("teams.id"))
    task_id: Mapped[Optional[int]] = mapped_column(ForeignKey("confirm_tasks.id"))
    to_email: Mapped[str] = mapped_column(String(200))
    subject: Mapped[str] = mapped_column(String(300), default="")
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued | retry_wait | sending | sent | skipped | failed
    # 발송 선점: 수동 실행·스케줄러·다른 프로세스가 같은 작업을 동시에 보내지 않도록 queued→sending 을 원자적으로 바꾼 쪽만 보낸다
    claimed_at: Mapped[Optional[datetime]] = mapped_column(default=None)
    claimed_by: Mapped[str] = mapped_column(String(100), default="", server_default="")
    attempts: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=3)
    scheduled_at: Mapped[datetime] = mapped_column(default=_now)
    sent_at: Mapped[Optional[datetime]] = mapped_column(default=None)
    last_error: Mapped[str] = mapped_column(String(300), default="")
    note: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(default=_now)


class DevMail(Base):
    """모의 발송 모드의 개발용 수신함(실제 Slack DM 대신 여기에 기록). to_email = 받는 사람 Slack ID."""

    __tablename__ = "dev_inbox"
    id: Mapped[int] = mapped_column(primary_key=True)
    email_job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("email_jobs.id"))
    to_email: Mapped[str] = mapped_column(String(200), index=True)
    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class Settlement(Base):
    """팀별 정산 결과. 다시 계산하면 새 version 의 draft 가 생기고 이전 draft 는 superseded."""

    __tablename__ = "settlements"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    version: Mapped[int]
    status: Mapped[str] = mapped_column(String(20))  # draft | confirmed | superseded
    final_fee: Mapped[int]
    received: Mapped[int]
    refunds_paid: Mapped[int]
    balance: Mapped[int]  # = final_fee - received + refunds_paid (양수: 추가 납부, 음수: 추가 환불)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # lines / receipts / rules
    blockers: Mapped[list[str]] = mapped_column(JSON, default=list)
    computed_at: Mapped[datetime] = mapped_column(default=_now)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(default=None)
    confirmed_by: Mapped[Optional[int]] = mapped_column(ForeignKey("admins.id"))
    manager_ack_at: Mapped[Optional[datetime]] = mapped_column(default=None)
    manager_ack_by: Mapped[Optional[int]] = mapped_column(ForeignKey("participants.id"))


class Payout(Base):
    """총무가 실제로 송금한 환불 결과 기록(송금 자체는 앱 밖에서)."""

    __tablename__ = "payouts"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    settlement_id: Mapped[Optional[int]] = mapped_column(ForeignKey("settlements.id"))
    amount: Mapped[int]
    memo: Mapped[str] = mapped_column(String(300), default="")
    recorded_by: Mapped[int] = mapped_column(ForeignKey("admins.id"))
    recorded_at: Mapped[datetime] = mapped_column(default=_now)


class DbMeta(Base):
    """DB 자체에 대한 표시. 시드로 만든 시연용 DB 는 purpose='demo-seed' — 초기화 명령은 이 표시가 있는 DB 만 지운다."""

    __tablename__ = "db_meta"
    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[Optional[int]] = mapped_column(ForeignKey("events.id"), index=True)
    actor: Mapped[str] = mapped_column(String(60))  # admin:1 | participant:12 | manager:12 | agent:settlement
    action: Mapped[str] = mapped_column(String(60))
    target: Mapped[str] = mapped_column(String(100), default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class TeamBilling(Base):
    """납부 마감 시점의 팀별 청구표(한 번 저장 후 수정 금지).
    담당자가 '이만큼 걷어서 내 주세요'라고 안내받은 금액이고, 마감 후 변동은 이 표와의 차이로 보고서에 나온다."""

    __tablename__ = "team_billings"
    __table_args__ = (UniqueConstraint("team_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    billed: Mapped[int] = mapped_column(default=0)  # 청구액(미확정 인원은 0으로 계산하고 unresolved 로 따로 셈)
    counts: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)  # bus/car/absent/undecided/none
    unresolved: Mapped[int] = mapped_column(default=0)
    lines: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # [{participant_id, name, choice, fee}]
    received_by_deadline: Mapped[int] = mapped_column(default=0)  # 마감까지 입금(배정 확정·제안 포함, 참고값)
    captured_at: Mapped[datetime] = mapped_column(default=_now)


class SettlementReport(Base):
    """정산 Agent 가 만든 정산 보고서. 기준표(마감 기록) + 변동 기록으로 다시 만들 때마다 새 버전으로 저장한다.
    내용이 이전 버전과 같으면 새 버전을 만들지 않는다(content_hash)."""

    __tablename__ = "settlement_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    version: Mapped[int]
    trigger: Mapped[str] = mapped_column(String(100))
    stage: Mapped[str] = mapped_column(String(30))  # collecting | attend_closed | payment_closed | event_done
    content_hash: Mapped[str] = mapped_column(String(64))
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    teams: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    people: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    diff: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # 이전 버전 대비 바뀐 팀·사람
    created_at: Mapped[datetime] = mapped_column(default=_now)


class AdminChat(Base):
    """총무와 총무용 Agent 의 대화. state/pending 은 마지막 메시지 meta 대신 여기 둔다(행사당 1개)."""

    __tablename__ = "admin_chats"
    __table_args__ = (UniqueConstraint("event_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    state: Mapped[str] = mapped_column(String(30), default="idle")  # idle | awaiting_confirmation
    pending: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, default=None)
    updated_at: Mapped[datetime] = mapped_column(default=_now)


class AdminChatMessage(Base):
    __tablename__ = "admin_chat_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("admin_chats.id"), index=True)
    sender: Mapped[str] = mapped_column(String(20))  # user | agent
    text: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=_now)
