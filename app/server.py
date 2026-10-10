"""총무봇 Slack 연동 서버 (로컬 실행용, 표준 라이브러리만 사용)

demo/index.html 이 이 서버를 통해 실제 Slack 워크스페이스의 멤버를 불러오고
채널을 만들어 초대합니다. 봇 토큰은 브라우저로 보내지 않고 이 서버만 가집니다.

실행:  python3 app/server.py        (기본 http://127.0.0.1:8787)

API
  GET  /api/slack/members                 워크스페이스 이름 + 사람 멤버 목록
  POST /api/slack/channel  {name, users}  채널 생성 후 users 초대
  POST /api/slack/invite   {channel, users} 기존 채널에 추가 초대
"""
import json
import os
import ssl
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env():
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
OWNER = os.environ.get("SLACK_OWNER_USER_ID", "")  # 총무 본인의 Slack user ID (U로 시작)
PORT = int(os.environ.get("PORT", "8787"))
# 데모 페이지가 열리는 곳만 허용 (GitHub Pages, 로컬 서버, file:// 은 Origin 이 "null")
ALLOWED_ORIGINS = {"https://greatdh84.github.io", "null"}


# python.org 설치본(macOS)은 인증서가 없어 HTTPS 가 실패하므로 시스템 인증서로 대체
SSL_CTX = ssl.create_default_context()
if not SSL_CTX.get_ca_certs() and Path("/etc/ssl/cert.pem").exists():
    SSL_CTX = ssl.create_default_context(cafile="/etc/ssl/cert.pem")


class SlackError(Exception):
    pass


def slack(method, **params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        f"https://slack.com/api/{method}", data=data,
        headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as r:
        body = json.load(r)
    if not body.get("ok"):
        raise SlackError(body.get("error", "unknown_error"))
    return body


def list_members():
    team = slack("auth.test").get("team", "")
    members, cursor = [], ""
    while True:
        page = slack("users.list", limit=200, cursor=cursor)
        for u in page["members"]:
            if u.get("deleted") or u.get("is_bot") or u["id"] == "USLACKBOT":
                continue
            p = u.get("profile", {})
            members.append({
                "id": u["id"],
                "name": p.get("display_name") or p.get("real_name") or u.get("name"),
                "image": p.get("image_72", ""),
            })
        cursor = page.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            break
    return {"team": team, "owner": OWNER or None, "members": members}


def invite(channel, users):
    users = [u for u in users if u]
    if users:
        try:
            slack("conversations.invite", channel=channel, users=",".join(users), force="true")
        except SlackError as e:
            if str(e) != "already_in_channel":
                raise
    return {"channel": channel, "invited": len(users)}


def create_channel(name, users):
    ch = slack("conversations.create", name=name)["channel"]
    invite(ch["id"], users)
    return {"channel": ch["id"], "name": ch["name"], "invited": len(users)}


class Handler(BaseHTTPRequestHandler):
    def cors(self):
        origin = self.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS or origin.startswith(("http://localhost", "http://127.0.0.1")):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Private-Network", "true")

    def reply(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.end_headers()

    def do_GET(self):
        self.handle_api(lambda: list_members() if self.path == "/api/slack/members" else None)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self.reply(400, {"error": "bad_json"})
        routes = {
            "/api/slack/channel": lambda: create_channel(data.get("name", ""), data.get("users", [])),
            "/api/slack/invite": lambda: invite(data.get("channel", ""), data.get("users", [])),
        }
        self.handle_api(routes.get(self.path, lambda: None))

    def handle_api(self, fn):
        if not TOKEN:
            return self.reply(500, {"error": "no_token"})
        try:
            out = fn()
        except SlackError as e:
            return self.reply(400, {"error": str(e)})
        except Exception as e:  # 네트워크 오류 등
            return self.reply(502, {"error": f"server_error: {e}"})
        if out is None:
            return self.reply(404, {"error": "not_found"})
        self.reply(200, out)


if __name__ == "__main__":
    if not TOKEN:
        print("⚠️  .env 에 SLACK_BOT_TOKEN 이 없습니다.")
    print(f"총무봇 Slack 서버: http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
