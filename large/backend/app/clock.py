"""시간 유틸. DB에는 naive UTC 로 저장하고, 발송 금지 시간·날짜 경계는 행사 시간대(KST)로 판단한다.
테스트는 clock.freeze() 로 현재 시각을 고정할 수 있다."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
_frozen: datetime | None = None


def freeze(dt: datetime | None) -> None:
    global _frozen
    _frozen = to_utc_naive(dt) if dt is not None else None


def now() -> datetime:
    """현재 시각(naive UTC)."""
    if _frozen is not None:
        return _frozen
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_utc_naive(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def kst(dt: datetime) -> datetime:
    """naive UTC → aware KST."""
    return dt.replace(tzinfo=timezone.utc).astimezone(KST)


def kst_naive_to_utc(dt: datetime) -> datetime:
    """KST 기준으로 입력된 naive 시각 → naive UTC."""
    return dt.replace(tzinfo=KST).astimezone(timezone.utc).replace(tzinfo=None)


def fmt(dt: datetime | None) -> str | None:
    """API 응답용 ISO 문자열(KST 오프셋 포함)."""
    return kst(dt).isoformat(timespec="seconds") if dt else None


def parse_input(value: str) -> datetime:
    """API 입력 ISO 문자열 → naive UTC. 오프셋이 없으면 KST 로 본다."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return kst_naive_to_utc(dt)
    return to_utc_naive(dt)


def kst_date_key(dt: datetime) -> str:
    return kst(dt).strftime("%Y-%m-%d")


def next_allowed_send_time(dt: datetime, quiet_start: int, quiet_end: int) -> datetime | None:
    """dt(naive UTC)가 발송 금지 시간이면 다음 허용 시각(naive UTC)을, 아니면 None."""
    local = kst(dt)
    h = local.hour
    in_quiet = (h >= quiet_start or h < quiet_end) if quiet_start > quiet_end else (quiet_start <= h < quiet_end)
    if not in_quiet:
        return None
    target = local.replace(hour=quiet_end, minute=0, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return to_utc_naive(target)
