"""공용 서버: 라우팅, SMALL 동작 보존(원래 서버와 비교), import 부작용, LARGE↔SMALL 분리."""
import json
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def small(monkeypatch):
    """SMALL 모듈. 개발자 PC 의 .env 에 실제 키가 있어도 테스트가 Slack·LLM·카카오를 부르지 않게 막는다."""
    from hub.small_bridge import small as mod

    for name in ("TOKEN", "APP_TOKEN", "LLM_KEY", "KAKAO_KEY", "KAKAO_JS_KEY"):
        monkeypatch.setattr(mod, name, "")

    def no_network(*a, **k):
        raise AssertionError("테스트 중 외부 네트워크 호출")

    monkeypatch.setattr(mod.urllib.request, "urlopen", no_network)
    return mod


@pytest.fixture
def fake_dist(tmp_path):
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text('<!doctype html><div id="root"></div><script src="/large/assets/app.js"></script>', encoding="utf-8")
    (d / "assets" / "app.js").write_text("console.log('large')", encoding="utf-8")
    return d


@pytest.fixture
def hub(small, fake_dist):
    from hub.main import create_app

    with TestClient(create_app(large_dist=fake_dist, start_small_socket=False), follow_redirects=False) as c:
        yield c


def test_import_has_no_side_effects():
    """SMALL 서버 모듈을 import 해도 스레드·네트워크 연결·서버가 시작되지 않는다."""
    code = (
        "import socket, threading\n"
        "def boom(*a, **k): raise RuntimeError('network during import')\n"
        "socket.socket.connect = boom\n"
        "n = threading.active_count()\n"
        "import hub.main as m\n"
        "s = m.small_bridge.small\n"
        "assert threading.active_count() == n, 'thread started'\n"
        "assert s.SOCKET == {'connected': False, 'error': ''}, s.SOCKET\n"
        "print('ok')\n"
    )
    env = {"PATH": "/usr/bin:/bin", "DATABASE_URL": "sqlite:///:memory:", "APP_MODE": "demo", "SLACK_APP_TOKEN": "xapp-fake", "SLACK_BOT_TOKEN": "xoxb-fake"}
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().endswith("ok")


def test_routes(hub):
    assert hub.get("/").headers["location"].endswith("/demo/index.html")
    assert hub.get("/small/").headers["location"] == "/demo/index.html"
    assert hub.get("/small").headers["location"] == "/demo/index.html"
    r = hub.get("/demo/index.html")
    assert r.status_code == 200 and "대모임 — 채총 영역" in r.text
    assert hub.get("/large").headers["location"].endswith("/large/")
    for path in ("/large/", "/large/admin", "/large/manager/login", "/large/invite"):  # 내부 경로 직접 접근·새로고침
        r = hub.get(path)
        assert r.status_code == 200 and '<div id="root">' in r.text, path
    assert hub.get("/large/assets/app.js").text == "console.log('large')"
    assert hub.get("/large/assets/missing.js").status_code == 404  # 없는 자산을 index.html 로 감싸지 않음
    assert hub.get("/api/large/meta").json()["system"] == "large"
    r = hub.get("/api/large/no-such")
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    assert hub.get("/api/hub").json()["large"]["ui_built"] is True


def test_large_ui_missing_build_is_explicit(small, tmp_path):
    from hub.main import create_app

    with TestClient(create_app(large_dist=tmp_path / "nope", start_small_socket=False)) as c:
        r = c.get("/large/admin")
        assert r.status_code == 503 and r.json()["error"]["code"] == "LARGE_UI_NOT_BUILT"


@pytest.fixture
def original_small(small):
    """원래 방식(app/server.py 의 Handler + ThreadingHTTPServer)으로 띄운 SMALL 서버."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), small.Handler)
    srv.RequestHandlerClass.log_message = lambda *a, **k: None
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _orig(base, method, path, body=None, headers=None):
    req = Request(base + path, data=body, method=method, headers=headers or {})

    class NoRedirect(__import__("urllib.request").request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = __import__("urllib.request").request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except HTTPError as e:
        return e.code, dict(e.headers), e.read()


CASES = [
    ("GET", "/", None, {}),
    ("GET", "/demo/index.html", None, {}),
    ("GET", "/demo/assets/hero.png", None, {}),
    ("GET", "/demo/../app/server.py", None, {}),  # demo 밖 파일 차단 유지
    ("GET", "/api/config", None, {}),
    ("GET", "/api/socket", None, {}),
    ("GET", "/api/survey?channel=C1", None, {}),
    ("GET", "/api/slack/members", None, {}),
    ("GET", "/api/no-such", None, {}),
    ("POST", "/api/slack/send", b'{"channel":"C1","text":"hi"}', {"Content-Type": "application/json"}),
    ("POST", "/api/pay/parse", b"{}", {"Content-Type": "application/json"}),
    ("POST", "/api/slack/channel", b"not json", {"Content-Type": "application/json"}),
    ("OPTIONS", "/api/slack/send", None, {"Origin": "http://127.0.0.1:8787"}),
    ("OPTIONS", "/api/slack/send", None, {"Origin": "https://evil.example"}),
]


@pytest.mark.parametrize("method,path,body,headers", CASES)
def test_small_behaves_like_original_server(hub, original_small, method, path, body, headers):
    o_status, o_headers, o_body = _orig(original_small, method, path, body, headers)
    r = hub.request(method, path, content=body, headers=headers)
    assert r.status_code == o_status
    assert r.content == o_body
    for h in ("Content-Type", "Location", "Access-Control-Allow-Origin", "Cache-Control"):
        assert r.headers.get(h) == o_headers.get(h), h


def test_large_traffic_never_reaches_small_slack(hub, small, monkeypatch):
    calls = []
    monkeypatch.setattr(small, "slack", lambda *a, **k: calls.append(a) or {})
    monkeypatch.setattr(small, "send", lambda *a, **k: calls.append(("send",)) or {})
    h = {"X-Requested-With": "chongmu"}
    hub.get("/large/")
    hub.get("/large/admin")
    hub.get("/api/large/meta")
    hub.post("/api/large/auth/admin/login", json={"email": "admin@example.com", "password": "demo1234!"}, headers=h)
    hub.get("/api/large/admin/events")
    hub.post("/api/large/auth/invite/exchange", json={"token": "x" * 43}, headers=h)
    hub.post("/api/large/me/chat", json={"text": "버스 말고 자차"}, headers=h)
    assert calls == []


def test_large_session_cookie_is_scoped_to_large_api(hub):
    r = hub.post("/api/large/auth/admin/login", json={"email": "admin@example.com", "password": "demo1234!"}, headers={"X-Requested-With": "chongmu"})
    assert r.status_code == 200
    assert "Path=/api/large" in r.headers["set-cookie"]


def test_large_csrf_rule_does_not_apply_to_small(hub):
    # SMALL 화면은 X-Requested-With 를 보내지 않는다. LARGE 의 CSRF 헤더 규칙이 SMALL 을 막으면 안 됨
    r = hub.post("/api/slack/send", json={"text": "x"})
    assert r.status_code != 403
    assert hub.post("/api/large/me/chat", json={"text": "x"}).json()["error"]["code"] == "CSRF_HEADER_MISSING"
