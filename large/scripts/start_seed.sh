#!/usr/bin/env bash
# 정산 보고서·총무 Agent 시연용: 시드 DB(large/backend/var/chongmu.db, 가짜 300명·행사 다음 날 상태)로 공용 서버 실행.
#   large/scripts/start_seed.sh           # 시드 DB 가 없으면 만들고 실행
#   large/scripts/start_seed.sh --reset   # 시드를 다시 만든 뒤 실행(app.seed --reset: 시연용 표시가 있는 DB 만 지움)
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
refuse_if_running

DB="$VAR/chongmu.db"
export DATABASE_URL="sqlite:///$DB"
if [[ "${1:-}" == "--reset" ]]; then
  (cd "$ROOT/large/backend" && "$PY" -m app.seed --reset)
elif [[ -n "${1:-}" ]]; then
  echo "알 수 없는 인자: $1 (쓸 수 있는 것: --reset)" >&2; exit 2
elif [[ ! -f "$DB" ]]; then
  (cd "$ROOT/large/backend" && "$PY" -m app.seed)
fi
show_settings "seed (시드 데이터 · 정산 보고서·총무 Agent 시연)"
cd "$ROOT"
PORT="$PORT" exec "$PY" -m hub
