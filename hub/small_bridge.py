"""SMALL(app/server.py)을 고치지 않고 공용 서버에 연결하는 브리지.

app/server.py 는 표준 라이브러리 http.server 기반이고 요청 처리가 Handler.do_GET/do_POST 에 모여 있다.
여기서는 그 Handler 를 소켓 없이 만들어 같은 메서드를 호출하고, 써 낸 HTTP 응답을 그대로 돌려준다.
→ SMALL 의 경로·응답 형식·CORS·정적 파일 제공(/ → /demo/index.html)이 원래 서버와 똑같이 유지된다.

import 부작용(app/server.py 모듈 최상위): 저장소 루트 .env 를 환경변수로 읽고(이미 있는 값은 유지),
SSL 인증서 경로를 정하고, 상수를 읽는 것뿐이다. Slack Socket Mode 연결과 HTTP 서버 시작은
app/server.py 의 `if __name__ == "__main__":` 안에만 있어 import 로는 실행되지 않는다.
(Socket Mode 는 공용 서버 시작 시 hub/main.py 가 명시적으로 start_socket() 을 부를 때만 켜진다.)
"""
from __future__ import annotations

import http.client
import importlib.util
import io
from pathlib import Path
from types import ModuleType

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response

ROOT = Path(__file__).resolve().parent.parent
SMALL_SERVER = ROOT / "app" / "server.py"
SMALL_METHODS = ("GET", "POST", "OPTIONS")  # app/server.py 가 구현한 메서드(그 밖은 원래 서버처럼 501)
_DROP_HEADERS = {"server", "date", "content-length"}


def load_small_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("chongmubot_small_server", SMALL_SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


small = load_small_module()


class _BridgeHandler(small.Handler):
    """소켓 없이 만든 SMALL Handler. rfile/wfile 을 메모리 버퍼로 둔다."""

    def __init__(self, method: str, target: str, headers: list[tuple[str, str]], body: bytes):  # noqa: D401 - 부모 __init__ 은 소켓 처리를 하므로 부르지 않음
        self.command, self.path = method, target
        self.request_version = "HTTP/1.1"
        self.requestline = f"{method} {target} HTTP/1.1"
        self.client_address = ("127.0.0.1", 0)
        self.server = None
        self.close_connection = True
        self.rfile, self.wfile = io.BytesIO(body), io.BytesIO()
        msg = http.client.HTTPMessage()
        for k, v in headers:
            msg[k] = v
        self.headers = msg

    def log_message(self, format, *args):  # noqa: A002 - 요청 로그는 공용 서버(uvicorn)가 남긴다
        pass


def call_small(method: str, target: str, headers: list[tuple[str, str]], body: bytes) -> tuple[int, list[tuple[str, str]], bytes]:
    h = _BridgeHandler(method, target, headers, body)
    getattr(h, "do_" + method)()
    raw = h.wfile.getvalue()
    head, _, payload = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    out_headers = [tuple(line.split(": ", 1)) for line in lines[1:] if ": " in line]
    return status, out_headers, payload


async def small_endpoint(request: Request) -> Response:
    if request.method not in SMALL_METHODS:
        return Response(status_code=501)
    # 원래 서버가 받던 그대로의 요청 줄(퍼센트 인코딩 유지)을 넘긴다
    target = request.scope.get("raw_path", request.url.path.encode()).decode("latin-1")
    if request.url.query:
        target += "?" + request.url.query
    body = await request.body()
    # SMALL 처리기는 동기 코드(urllib, time.sleep)라 스레드에서 실행
    status, headers, payload = await run_in_threadpool(call_small, request.method, target, list(request.headers.items()), body)
    resp = Response(content=payload, status_code=status)
    for k, v in headers:
        if k.lower() not in _DROP_HEADERS:
            resp.headers.append(k, v)
    return resp
