# start_live.sh · start_seed.sh 공용: 저장소 루트·파이썬 찾기, 켜진 설정 출력
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VAR="$ROOT/large/backend/var"
PY="${PYTHON:-}"
if [[ -z "$PY" ]]; then
  if [[ -x "$ROOT/.venv/bin/python" ]]; then PY="$ROOT/.venv/bin/python"; else PY="$(command -v python3)"; fi
fi

PORT="${PORT:-8787}"

# 이미 공용 서버가 켜져 있으면 멈춤(켜진 서버가 쓰는 DB 를 지우거나 포트가 겹치지 않게)
refuse_if_running() {
  if command -v lsof >/dev/null 2>&1; then
    local pid; pid="$(lsof -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)"
    if [[ -n "$pid" ]]; then
      echo "이미 127.0.0.1:$PORT 에 서버가 켜져 있습니다(PID $pid). 그 터미널에서 Ctrl+C 로 끈 뒤 다시 실행하세요." >&2
      exit 1
    fi
  fi
}

# 실제로 적용될 값(환경변수 → large/backend/.env → 저장소 루트 .env 순)을 대모임 설정 코드로 읽어 보여 준다
show_settings() {
  local label="$1"
  echo "────────────────────────────────────────────"
  echo " 대모임 DB: $label"
  (cd "$ROOT/large/backend" && "$PY" -c 'from app.config import settings as s; print(f" DATABASE_URL={s.database_url}\n NOTIFY_MODE={s.email_mode}  LLM_MODE={s.llm_mode}  APP_MODE={s.app_mode}")')
  echo " 화면: http://127.0.0.1:$PORT → 시작하기 → 워크샵"
  echo "────────────────────────────────────────────"
}
