"""소통 Agent 가 쓰는 도구와 입출력 계약.

- 모든 도구는 ToolContext(로그인한 본인·팀)로 실행되며, 접근 통제는 여기 코드에서 한다.
  LLM 이 다른 참가자·팀 ID 를 넣어도 본인/자기 팀 범위를 벗어나면 FORBIDDEN.
- mutating=True 인 도구는 LLM 이 호출해도 바로 실행되지 않는다. 오케스트레이터가 먼저 검증(dry_run)하고
  사용자에게 확인을 받은 뒤(대화 상태 awaiting_confirmation) 실행한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..errors import AppError, forbidden, invalid, not_found
from ..models import CHOICE_LABELS, ChangeLog, ChatThread, ConfirmTask, Event, Participant, RefundRule, Response, Team
from ..services import responses as resp_svc
from ..services.common import audit, create_task, is_active_manager
from ..services.settlement import compute_team


@dataclass
class ToolContext:
    db: Session
    event: Event
    actor: Participant
    role: str  # participant | manager
    thread: ChatThread
    message_id: int | None = None


@dataclass
class ToolSpec:
    name: str
    description: str
    roles: tuple[str, ...]
    input_schema: dict[str, Any]
    output: str
    run: Callable[[ToolContext, dict[str, Any]], dict[str, Any]]
    mutating: bool = False
    dry_run: Callable[[ToolContext, dict[str, Any]], dict[str, Any]] | None = None

    def contract(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema, "output": self.output, "mutating": self.mutating}


def _check_target(ctx: ToolContext, args: dict[str, Any]) -> None:
    target = args.get("target")
    if target not in (None, "", "self", ctx.actor.name):
        raise forbidden("본인의 응답만 확인·변경할 수 있습니다")


# ---------------------------------------------------------------- 참가자 도구

def get_my_status(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    p, ev = ctx.actor, ctx.event
    st = resp_svc.fee_table(ctx.db, ev, p.team_id)[p.id]
    pc = ctx.db.scalar(select(ChangeLog).where(ChangeLog.participant_id == p.id, ChangeLog.status == "pending_approval"))
    return {
        "name": p.name,
        "team": ctx.db.get(Team, p.team_id).name,
        "choice": st.choice,
        "choice_label": CHOICE_LABELS[st.choice],
        "fee": st.liability,
        "after_attend_deadline": resp_svc.after_attend_deadline(ev),
        "attend_deadline": clock.kst(ev.attend_deadline).strftime("%Y-%m-%d %H:%M"),
        "payment_deadline": clock.kst(ev.payment_deadline).strftime("%Y-%m-%d %H:%M"),
        "pending_change": {"change_id": pc.id, "from": pc.from_choice, "to": pc.to_choice} if pc else None,
    }


def get_fee_rules(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    ev = ctx.event
    rules = ctx.db.scalars(select(RefundRule).where(RefundRule.event_id == ev.id).order_by(RefundRule.change_type, RefundRule.valid_until))
    return {
        "fees": {"bus": ev.fee_bus, "car": ev.fee_car, "absent": ev.fee_absent},
        "refund_rules": [
            {"change_type": r.change_type, "valid_until": clock.fmt(r.valid_until), "refund_percent": r.refund_percent, "description": r.description}
            for r in rules
        ],
    }


def preview_change(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    _check_target(ctx, args)
    return resp_svc.preview_change(ctx.db, ctx.event, ctx.actor, args["choice"])


def request_change(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    _check_target(ctx, args)
    change = resp_svc.submit_change(
        ctx.db, ctx.event, ctx.actor, args["choice"], source="chat", actor=f"participant:{ctx.actor.id}", evidence_message_id=ctx.message_id
    )
    return {"change_id": change.id, "status": change.status, "to_choice": change.to_choice, "refund_amount": change.refund_amount, "extra_charge": change.extra_charge, "rule": change.rule_text}


# ---------------------------------------------------------------- 담당자 도구

def _require_manager(ctx: ToolContext) -> None:
    if ctx.role != "manager" or not is_active_manager(ctx.db, ctx.actor.id, ctx.thread.team_id):
        raise forbidden("팀 담당자만 사용할 수 있습니다")


def team_counts(db: Session, team_id: int) -> dict[str, int]:
    counts = {"bus": 0, "car": 0, "absent": 0, "undecided": 0, "none": 0}
    pids = list(db.scalars(select(Participant.id).where(Participant.team_id == team_id)))
    answered = {r.participant_id: r.choice for r in db.scalars(select(Response).where(Response.participant_id.in_(pids)))}
    for pid in pids:
        counts[answered.get(pid, "none")] += 1
    return counts


def get_team_summary(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    _require_manager(ctx)
    team = ctx.db.get(Team, ctx.thread.team_id)
    calc = compute_team(ctx.db, ctx.event, team)
    return {k: calc[k] for k in ("team_name", "final_fee", "received", "refunds_paid", "balance", "balance_label", "blockers")} | {"counts": team_counts(ctx.db, team.id)}


def _open_manager_tasks(ctx: ToolContext) -> list[ConfirmTask]:
    return list(
        ctx.db.scalars(
            select(ConfirmTask).where(ConfirmTask.team_id == ctx.thread.team_id, ConfirmTask.assignee == "manager", ConfirmTask.status == "open").order_by(ConfirmTask.id)
        )
    )


def list_open_tasks(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    _require_manager(ctx)
    return {"tasks": [{"task_id": t.id, "kind": t.kind, "question": t.question, "options": t.options} for t in _open_manager_tasks(ctx)]}


def _get_answerable_task(ctx: ToolContext, args: dict[str, Any]) -> ConfirmTask:
    _require_manager(ctx)
    task = ctx.db.get(ConfirmTask, int(args["task_id"]))
    if task is None or task.team_id != ctx.thread.team_id or task.assignee != "manager":
        raise not_found("확인 과제")
    if task.status != "open":
        raise AppError(409, "TASK_NOT_OPEN", f"확인 과제 #{task.id}는 이미 답변·처리되었습니다")
    if args["option"] not in {o["key"] for o in task.options}:
        raise invalid("INVALID_OPTION", "선택할 수 없는 답변입니다")
    return task


def answer_task_dry(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    task = _get_answerable_task(ctx, args)
    label = next(o["label"] for o in task.options if o["key"] == args["option"])
    return {"task_id": task.id, "question": task.question, "option": args["option"], "option_label": label}


def answer_task(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from .settlement_agent import review_task  # 순환 import 방지

    task = _get_answerable_task(ctx, args)
    task.status, task.answer, task.answer_note = "answered", args["option"], (args.get("note") or "")[:500]
    task.answered_by, task.answered_at, task.answer_message_id = ctx.actor.id, clock.now(), ctx.message_id
    task.updated_at = clock.now()
    audit(ctx.db, ctx.event.id, f"manager:{ctx.actor.id}", "task_answered", f"task:{task.id}", {"option": args["option"]})
    review = review_task(ctx.db, ctx.event, task, trigger=f"task_answered:{task.id}")
    return {"task_id": task.id, "status": task.status, "review": review}


def request_correction(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    _require_manager(ctx)
    text = (args.get("text") or "").strip()
    if not text:
        raise invalid("EMPTY_TEXT", "정정 요청 내용을 적어 주세요")
    task, _ = create_task(
        ctx.db,
        dedupe_key=f"correction:{ctx.thread.team_id}:{ctx.message_id or clock.now().isoformat()}",
        event_id=ctx.event.id,
        kind="correction_request",
        assignee="admin",
        team_id=ctx.thread.team_id,
        participant_id=ctx.actor.id,
        question=f"{ctx.db.get(Team, ctx.thread.team_id).name} 담당자 정정 요청: {text[:500]}",
        options=[{"key": "done", "label": "처리 완료"}],
        evidence={"message_id": ctx.message_id},
        created_by="communication_agent",
    )
    return {"task_id": task.id}


CHOICE_SCHEMA = {"type": "string", "enum": ["bus", "car", "absent", "undecided"]}
TARGET_SCHEMA = {"type": "string", "description": "변경 대상. 본인이면 생략 또는 'self'. 다른 사람 이름이면 거절됨"}

TOOLS: dict[str, ToolSpec] = {
    t.name: t
    for t in [
        ToolSpec("get_my_status", "본인의 현재 응답·회비·마감·승인 대기 변경 조회", ("participant",), {"type": "object", "properties": {}}, "{name, team, choice, choice_label, fee, attend_deadline, payment_deadline, pending_change}", get_my_status),
        ToolSpec("get_fee_rules", "행사 회비와 마감 후 변경 환불 규칙 조회", ("participant", "manager"), {"type": "object", "properties": {}}, "{fees:{bus,car,absent}, refund_rules:[{change_type, valid_until, refund_percent, description}]}", get_fee_rules),
        ToolSpec("preview_change", "응답 변경 시 회비·환불을 기록 없이 미리 계산", ("participant",), {"type": "object", "properties": {"choice": CHOICE_SCHEMA, "target": TARGET_SCHEMA}, "required": ["choice"]}, "{from_choice, to_choice, after_deadline, requires_approval, fee_before, fee_after, refund_amount, extra_charge, rule_text}", preview_change),
        ToolSpec("request_change", "본인 응답 변경 기록(마감 후엔 총무 승인 대기). 사용자 확인 후 실행됨", ("participant",), {"type": "object", "properties": {"choice": CHOICE_SCHEMA, "target": TARGET_SCHEMA}, "required": ["choice"]}, "{change_id, status: applied|pending_approval, refund_amount, extra_charge, rule}", request_change, mutating=True, dry_run=preview_change),
        ToolSpec("get_team_summary", "자기 팀 응답 현황·최종 회비·확정 수납액·잔액·정산 장애 요인", ("manager",), {"type": "object", "properties": {}}, "{team_name, counts, final_fee, received, refunds_paid, balance, balance_label, blockers}", get_team_summary),
        ToolSpec("list_open_tasks", "자기 팀에 온 열린 확인 요청 목록", ("manager",), {"type": "object", "properties": {}}, "{tasks:[{task_id, kind, question, options:[{key,label}]}]}", list_open_tasks),
        ToolSpec("answer_task", "확인 요청에 답변(정산 Agent 가 재검토). 사용자 확인 후 실행됨", ("manager",), {"type": "object", "properties": {"task_id": {"type": "integer"}, "option": {"type": "string"}, "note": {"type": "string"}}, "required": ["task_id", "option"]}, "{task_id, status, review}", answer_task, mutating=True, dry_run=answer_task_dry),
        ToolSpec("request_correction", "팀 명세 정정 요청을 총무에게 전달. 사용자 확인 후 실행됨", ("manager",), {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}, "{task_id}", request_correction, mutating=True, dry_run=lambda ctx, a: {"text": (a.get("text") or "")[:500]}),
    ]
}


def tools_for(role: str) -> list[ToolSpec]:
    return [t for t in TOOLS.values() if role in t.roles]


def validate_args(spec: ToolSpec, args: Any) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise invalid("BAD_TOOL_ARGS", "도구 인자는 객체여야 합니다")
    props = spec.input_schema.get("properties", {})
    clean: dict[str, Any] = {}
    for key in spec.input_schema.get("required", []):
        if key not in args or args[key] in (None, ""):
            raise invalid("BAD_TOOL_ARGS", f"필수 인자 누락: {key}")
    for key, schema in props.items():
        if key not in args or args[key] is None:
            continue
        v = args[key]
        if schema.get("type") == "integer":
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise invalid("BAD_TOOL_ARGS", f"{key} 는 정수여야 합니다") from None
        elif schema.get("type") == "string":
            v = str(v)
        if "enum" in schema and v not in schema["enum"]:
            raise invalid("BAD_TOOL_ARGS", f"{key} 값이 허용 범위가 아닙니다")
        clean[key] = v
    return clean


def run_tool(ctx: ToolContext, name: str, args: Any, *, dry: bool = False) -> dict[str, Any]:
    """도구 실행 결과를 {tool, args, ok, result|error} 로 돌려준다(예외를 LLM 이 읽을 수 있는 오류로 변환)."""
    spec = TOOLS.get(name)
    step: dict[str, Any] = {"tool": name, "args": args, "dry_run": dry}
    try:
        if spec is None or ctx.role not in spec.roles:
            raise forbidden(f"이 역할에서 쓸 수 없는 도구입니다: {name}")
        clean = validate_args(spec, args)
        step["args"] = clean
        fn = spec.dry_run if dry and spec.dry_run else spec.run
        step |= {"ok": True, "result": fn(ctx, clean)}
    except AppError as e:
        step |= {"ok": False, "error": {"code": e.code, "message": e.message}}
    return step
