#!/usr/bin/env bash
# 명단 업로드·실시간 DM 시연용: 빈 행사 DB(large/backend/var/live.db)로 공용 서버 실행.
#   large/scripts/start_live.sh           # live.db 가 없으면 빈 행사로 만들고 실행
#   large/scripts/start_live.sh --reset   # live.db 를 지우고 빈 행사로 새로 만든 뒤 실행
# 시드 DB(가짜 300명)와 섞이지 않게 따로 쓴다. 정산 보고서 시연은 start_seed.sh.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
refuse_if_running

DB="$VAR/live.db"
if [[ "${1:-}" == "--reset" ]]; then
  refuse_if_db_open "$DB"
  rm -f "$DB" "$DB-wal" "$DB-shm" "$DB-journal"
  echo "live.db 를 지웠습니다."
elif [[ -n "${1:-}" ]]; then
  echo "알 수 없는 인자: $1 (쓸 수 있는 것: --reset)" >&2; exit 2
fi
export DATABASE_URL="sqlite:///$DB"
if [[ ! -f "$DB" ]]; then
  (cd "$ROOT/large/backend" && "$PY" scripts/make_empty_event.py)
fi
show_settings "live (빈 행사 · 명단 업로드·초대/독촉 DM 시연)"
cd "$ROOT"
PORT="$PORT" exec "$PY" -m hub
