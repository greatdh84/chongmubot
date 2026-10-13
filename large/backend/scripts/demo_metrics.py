"""합성 시연 데이터에서 정산 처리(규칙 기반) 결과를 실측. 임시 DB 사용, 시연 DB 는 건드리지 않음.

    ../../.venv/bin/python scripts/demo_metrics.py [--base-date 2026-10-15]

시드에 일부러 넣은 이상 사례(정답)를 정산 처리가 어떻게 다뤘는지 센다. 숫자는 이 합성 데이터 1회 실행 결과일 뿐이다.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db as dbmod, seed as seedmod  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--base-date", default="2026-10-15")
args = ap.parse_args()
tmp = Path(tempfile.mkdtemp(prefix="demo-metrics-"))
seedmod.SAMPLES_DIR = tmp / "samples"
dbmod.configure(f"sqlite:///{tmp / 'm.db'}")
t0 = time.monotonic()
seedmod.seed(date.fromisoformat(args.base_date))
seed_sec = time.monotonic() - t0

from app.agents.settlement_agent import review_event  # noqa: E402
from app.models import Allocation, BankTransaction, ConfirmTask, Event  # noqa: E402

db = dbmod.SessionLocal()
txns = db.query(BankTransaction).all()
allocs = db.query(Allocation).all()
tasks = db.query(ConfirmTask).filter(ConfirmTask.kind != "change_approval").all()
by_txn = {t.id: t for t in txns}
alloc_txn = {a.transaction_id for a in allocs}
task_txn = Counter(t.transaction_id for t in tasks if t.transaction_id)


def txn_of(depositor, amount=None):
    return next(t for t in txns if t.depositor == depositor and (amount is None or t.amount == amount))


planted = {
    "입금자 불명 'ㅎㅂ' → 배정 안 함·후보 팀에 질문": (lambda t: t.id not in alloc_txn and task_txn[t.id] >= 2)(txn_of("ㅎㅂ")),
    "동명이인 '김민준' → 배정 안 함·두 팀에 질문": (lambda t: t.id not in alloc_txn and task_txn[t.id] == 2)(txn_of("김민준", 20000)),
    "결산이자 37원 → 총무 이관": any(t.kind == "unknown_depositor" and t.status == "escalated" and by_txn[t.transaction_id].amount == 37 for t in tasks if t.transaction_id),
    "T13 개인+팀 중복 입금 → 확인 과제": any(t.kind == "possible_duplicate" for t in tasks),
    "T08 개인 직접 입금 → 개인 배정 제안": any(a.participant_id and a.confidence == "medium" and "참가자 본인" in a.basis for a in allocs),
    "T06 메모 없는 담당자 입금 → 팀 배정 제안": any("담당자, 팀 표기 없음" in a.basis for a in allocs),
    "T22 분할 입금 2건 → 둘 다 같은 팀 제안": len([a for a in allocs if a.basis.startswith("메모/입금자에 팀 표기 'T22")]) == 2,
}
ev = db.query(Event).one()
t1 = time.monotonic()
review_event(db, ev, trigger="metrics-rerun")
db.rollback()
review_sec = time.monotonic() - t1

print(f"합성 데이터 실측 (기준일 {args.base_date}, 시드 20261015)")
print(f"- 거래 {len(txns)}건 중 배정 제안 {len(alloc_txn)}건 (신뢰도: {dict(Counter(a.confidence for a in allocs))}), 제안 없이 확인 과제로 간 거래 {len([t for t in txns if t.id not in alloc_txn])}건")
print(f"- 확인 과제(변경 승인 제외): {dict(Counter(t.kind for t in tasks))}")
print(f"- 일부러 넣은 이상 사례 {len(planted)}개 중 의도대로 처리 {sum(planted.values())}개")
for k, v in planted.items():
    print(f"    {'✓' if v else '✗'} {k}")
print(f"- 정산 처리 1회 재실행 시간 {review_sec:.2f}초 (300명·25팀, 이 PC), 시드 생성 {seed_sec:.1f}초")
