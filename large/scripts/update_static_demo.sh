#!/usr/bin/env bash
# 대모임 GitHub Pages 정적 데모판(demo/large/) 다시 만들기 — large/ 를 고친 뒤 한 번 실행.
#   large/scripts/update_static_demo.sh        (저장소 안 어디서 실행해도 됨)
# 1) 화면 정적 빌드(npm ci · build:static → demo/large/index.html, assets/)
# 2) 임시 DB 시드 + 응답 녹화(export_static_demo.py → demo/large/fixtures.json, report.csv). 실제 Slack·LLM 키는 쓰지 않음.
# 파이썬은 $PYTHON → 저장소 .venv → python3 순서로 찾는다. 중간에 실패하면 멈춘다.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="${PYTHON:-}"
if [[ -z "$PY" ]]; then
  if [[ -x "$ROOT/.venv/bin/python" ]]; then PY="$ROOT/.venv/bin/python"; else PY="$(command -v python3)"; fi
fi

echo "▶ 화면 정적 빌드 (large/frontend → demo/large/)"
cd "$ROOT/large/frontend"
npm ci --no-audit --no-fund
npm run build:static

echo "▶ 응답 녹화 (export_static_demo.py, $PY)"
cd "$ROOT/large/backend"
LARGE_SKIP_ROOT_ENV=1 NOTIFY_MODE=mock LLM_MODE=mock "$PY" scripts/export_static_demo.py --out "$ROOT/demo/large"

echo "✓ 정적 데모판 갱신 완료: demo/large/ (git status demo/large 로 바뀐 파일 확인)"
