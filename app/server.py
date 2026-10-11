"""총무봇 Slack 연동 서버 (로컬 실행용, 표준 라이브러리만 사용)

demo/index.html 이 이 서버를 통해 실제 Slack 워크스페이스의 멤버를 불러오고
채널을 만들어 초대합니다. 봇 토큰은 브라우저로 보내지 않고 이 서버만 가집니다.

실행:  python3 app/server.py        (기본 http://127.0.0.1:8787)

API
  GET  /api/slack/members                 워크스페이스 이름 + 사람 멤버 목록
  POST /api/slack/channel  {name, users}  채널 생성 후 users 초대
  POST /api/slack/invite   {channel, users} 기존 채널에 추가 초대
  POST /api/slack/send     {channel|user, text, persona, situation, name}
                           Claude 가 성격에 맞게 문구를 다시 쓰고(키 없거나 실패 시 text 그대로) 채널/DM 발송
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
LLM_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-haiku-5-5")
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


# 성격별 말투 지시 + Slack 에 보이는 보낸 사람 이름·아이콘
PERSONAS = {
    "active": ("활발", ":laughing:", "밝고 에너지 넘치는 말투. 느낌표와 이모지를 자주 쓴다."),
    "shy": ("수줍", ":pleading_face:", "소심하고 미안해하는 말투. 말끝을 흐리고(…) 조심스럽게 부탁한다."),
    "grumpy": ("까칠", ":unamused:", "짧고 건조한 말투. 용건만 말하고 단호하다. 욕설·모욕은 하지 않는다."),
}


def rewrite(text, persona, situation, name):
    """Claude 로 문구를 상황에 맞게 다시 쓴다. 실패하면 None (호출 측에서 원문 사용)"""
    if not LLM_KEY:
        return None
    label, _, style = PERSONAS.get(persona, PERSONAS["active"])
    system = (f"너는 모임 회비·참석을 관리하는 Slack 총무봇이다. 성격: {label} — {style}\n"
              "예시 문구의 사실(이름, 모임명, 금액, 마감)과 단계별 강도를 그대로 지키면서 같은 성격으로 새로 써라. "
              "한두 문장, Slack 메시지 본문만 출력한다.")
    body = json.dumps({
        "model": LLM_MODEL, "max_tokens": 1024, "system": system,
        "messages": [{"role": "user", "content": f"상황: {situation}\n대상: {name or '전체'}\n예시 문구: {text}"}],
    }).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": LLM_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
            out = json.load(r)
        # 응답 앞에 thinking 블록이 올 수 있어 text 블록만 모음
        return "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text").strip() or None
    except Exception as e:
        print("LLM 실패, 원문 사용:", e)
        return None


def send(data):
    persona = data.get("persona", "active")
    label, icon, _ = PERSONAS.get(persona, PERSONAS["active"])
    text = data.get("text", "")
    new = rewrite(text, persona, data.get("situation", ""), data.get("name", ""))
    channel = data.get("channel") or slack("conversations.open", users=data.get("user", ""))["channel"]["id"]
    slack("chat.postMessage", channel=channel, text=new or text, username=f"총무봇 · {label}", icon_emoji=icon)
    return {"text": new or text, "llm": bool(new)}


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
            "/api/slack/send": lambda: send(data),
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
