"""한 명령으로 공용 서버 실행:  .venv/bin/python -m hub"""
import os

import uvicorn

from .main import LARGE_DIST, small_bridge


def main() -> None:
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8787"))  # SMALL 의 카카오 지도 키가 127.0.0.1:8787 에 등록돼 있어 기본값 유지
    s = small_bridge.small
    if not s.TOKEN:
        print("⚠️  SMALL: .env 에 SLACK_BOT_TOKEN 이 없습니다 → 소모임은 데모 멤버로 동작(실제 Slack 발송 없음)")
    if not (LARGE_DIST / "index.html").exists():
        print("⚠️  LARGE: 화면 빌드가 없습니다 → cd large/frontend && npm ci && npm run build")
    print(f"총무봇 공용 서버: http://{host}:{port}  (/ 소모임·규모 선택, /large/ 대모임)")
    uvicorn.run("hub.main:app", host=host, port=port)


if __name__ == "__main__":
    main()
