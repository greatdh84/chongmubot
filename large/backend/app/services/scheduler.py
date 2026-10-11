"""이메일 발송 스케줄러: 서버 안의 백그라운드 스레드가 일정 간격으로 기존 발송 큐(dispatch_due)를 실행한다.

- 켜기/끄기: 환경변수 EMAIL_SCHEDULER=on|off (기본: 모의 메일이면 on, SMTP 면 off), 간격 EMAIL_SCHEDULER_INTERVAL(초, 기본 60)
  실행 중 일시정지·재개: 총무 화면(이메일 탭) 또는 POST /api/large/admin/events/{id}/emails/scheduler
- 발송 규칙(발송 직전 재검사·야간 제한·하루 한도·재시도·중복 방지)은 dispatch_due 가 그대로 적용한다.
- 겹침: 같은 프로세스는 잠금, 다른 프로세스는 DB 선점(queued→sending)으로 같은 메일을 두 번 보내지 않는다.
  남은 한계: 발송 중 프로세스가 죽으면 그 1건은 실제로 나갔는지 알 수 없다. 10분 뒤 failed + '확인 필요' 메모로 바꾸고
  자동 재발송하지 않는다(중복보다 누락 확인을 택함).
- 서버 종료 시 stop() 으로 스레드를 멈춘다(진행 중인 1회는 끝까지 처리).
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any

from sqlalchemy import select

from .. import clock
from ..config import settings
from ..db import SessionLocal
from ..models import Event
from . import emails

log = logging.getLogger("app.scheduler")


class EmailScheduler:
    def __init__(self, interval: int) -> None:
        self.interval = interval
        self.paused = False
        self.runs = 0
        self.last_run_at: datetime | None = None
        self.last_result: dict[str, Any] = {}
        self.last_error = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="large-email-scheduler", daemon=True)
        self._thread.start()
        log.info("이메일 스케줄러 시작: %s초 간격, 메일 모드 %s", self.interval, settings.email_mode)

    def stop(self, timeout: float = 10) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            if not self.paused:
                self.run_once()

    def run_once(self, now: datetime | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {}
        try:
            with SessionLocal() as db:
                for ev in db.scalars(select(Event).where(Event.mode == "large")).all():
                    r = emails.dispatch_due(db, ev, now=now, source="scheduler")
                    if r["processed"]:
                        result[str(ev.id)] = r
            self.last_error = ""
        except Exception as e:  # 한 번 실패해도 다음 주기에 다시 시도
            self.last_error = f"{type(e).__name__}: {e}"[:300]
            log.exception("이메일 스케줄러 실행 실패")
        self.runs += 1
        self.last_run_at = clock.now()
        self.last_result = result
        return result

    def status(self) -> dict[str, Any]:
        return {
            "configured": settings.email_scheduler,
            "running": self.running,
            "paused": self.paused,
            "enabled": self.running and not self.paused,
            "interval_seconds": self.interval,
            "runs": self.runs,
            "last_run_at": clock.fmt(self.last_run_at),
            "last_result": self.last_result,
            "last_error": self.last_error,
            "email_mode": settings.email_mode,
            "multi_process": "다른 프로세스와는 DB 선점으로 중복 발송 방지(단일 서버 권장)",
        }


scheduler = EmailScheduler(settings.email_scheduler_interval)
