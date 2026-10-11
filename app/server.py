"""총무봇 Slack 연동 서버 (로컬 실행용, 표준 라이브러리만 사용)

demo/index.html 이 이 서버를 통해 실제 Slack 워크스페이스의 멤버를 불러오고
채널을 만들어 초대합니다. 봇 토큰은 브라우저로 보내지 않고 이 서버만 가집니다.

실행:  app/.venv/bin/python app/server.py   (기본 http://127.0.0.1:8787)
       가상환경 준비: python3 -m venv app/.venv && app/.venv/bin/pip install slack_bolt
       slack_bolt 가 없거나 SLACK_APP_TOKEN 이 없으면 응답 받기(버튼·입력창)만 빠지고 나머지는 동작
       브라우저에서 http://127.0.0.1:8787 을 열면 데모 화면 (카카오 지도는 이 주소에서만 동작)

API
  GET  /api/config                       카카오 지도 JavaScript 키 (브라우저 공개용 키)
  GET  /api/slack/members                 워크스페이스 이름 + 사람 멤버 목록
  POST /api/slack/channel  {name, users}  채널 생성 후 users 초대
  POST /api/slack/invite   {channel, users} 기존 채널에 추가 초대
  POST /api/slack/send     {channel|user, text, persona, situation, name, survey_channel?}
                           Claude 가 성격대로 처음부터 짧은 메시지 1~3개로 써서 몇 초 간격으로 나눠 보냄
  POST /api/pay/parse      {text?, images?[dataURL]}  은행 입금 문자·캡처에서 입금자·금액·시각을 읽음
  POST /api/receipt/parse  {images[dataURL]}  영수증에서 최종 결제 금액과 가게 이름을 읽음
                           Claude 가 성격에 맞게 문구를 다시 쓰고(키 없거나 실패 시 text 그대로) 채널/DM 발송
  POST /api/places/recommend {area, food, prefs, drink}
                           카카오 로컬 검색 결과 중 Claude 가 조건에 맞는 후보를 골라 이유·예상 금액을 붙임
  GET  /api/places/search?q=  카카오맵 가게 검색 (직접 추가용 자동완성)
  GET  /api/places/nearby?x=&y=&r=  지도에서 클릭한 지점의 가장 가까운 음식점
  POST /api/slack/poll     {channel, title, options, deadline}  이모지 투표 메시지 + 번호 이모지 미리 달기
  POST /api/slack/thread   {channel, ts, text}  메시지 아래 스레드 댓글 (투표 마감 안내)
  POST /api/survey/start   {channel, title, text, questions}  [응답하기] 버튼이 달린 참석 조사 메시지
  GET  /api/survey?channel=  참석 조사 질문과 사람별 응답
  GET  /api/socket         응답 받기(Socket Mode) 연결 상태
  POST /api/slack/cancel   {channel, text}  모임 취소 안내, 참석 조사 기록 삭제 (채널은 그대로 둠)
  GET  /api/slack/poll?channel=&ts=  번호 이모지별 투표자 (봇 반응 제외)
"""
import json
import mimetypes
import random
import threading
import time
import os
import ssl
import urllib.error
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
# python.org 설치본(macOS)은 인증서가 없어 HTTPS·웹소켓이 실패하므로 시스템 인증서를 기본으로 (slack_sdk 포함)
if Path("/etc/ssl/cert.pem").exists():
    os.environ.setdefault("SSL_CERT_FILE", "/etc/ssl/cert.pem")
TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
OWNER = os.environ.get("SLACK_OWNER_USER_ID", "")  # 총무 본인의 Slack user ID (U로 시작)
APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")  # xapp-… Socket Mode (버튼·입력창 응답 받기)
LLM_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-haiku-5-5")
KAKAO_KEY = os.environ.get("KAKAO_REST_API_KEY", "")
KAKAO_JS_KEY = os.environ.get("KAKAO_JS_KEY", "")  # 지도 표시용, 등록한 도메인에서만 동작
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
# 성격별 말버릇과 예시 대화 (처음부터 쓸 때 말투가 일정하게)
TRAITS = {
    "active": ("'~요!', '~용', 'ㅎㅎ', 'ㅋㅋ'를 섞고 이모지를 메시지마다 1~3개. 느낌표 많이. 반말은 안 씀.",
               ['["도형님~ 좋은 아침이에요! ☀️", "회식 장소 투표 아직이시죠? ㅎㅎ", "이모지 하나만 콕! 오늘 6시 마감이에용 🙌"]']),
    "shy": ("'저기…', '혹시…', '죄송해요'로 시작하는 일이 많고 말끝을 '…'로 흐림. 이모지는 가끔 🥹🙏만.",
            ['["저기… 도형님…", "혹시 참석 조사… 시간 되실 때 눌러주실 수 있을까요…?", "마감이 오늘 4시라서요… 죄송해요 🙏"]']),
    "grumpy": ("마침표로 끝나는 짧은 평서문. 존댓말이지만 건조함. 인사·이모지는 거의 없고 써도 😑 정도. 모욕·욕설 금지.",
               ['["도형님.", "참석 조사 아직입니다.", "오늘 16시 마감. 버튼 하나면 됩니다."]']),
}


def compose(text, persona, situation, name):
    """전할 내용(사실)을 바탕으로 Claude 가 성격대로 짧은 메시지 1~3개를 처음부터 씀. 실패하면 None"""
    if not LLM_KEY:
        return None
    label, _, style = PERSONAS.get(persona, PERSONAS["active"])
    traits, examples = TRAITS.get(persona, TRAITS["active"])
    now = time.localtime()
    part = "아침" if 5 <= now.tm_hour < 11 else "점심" if now.tm_hour < 14 else "오후" if now.tm_hour < 18 else "저녁" if now.tm_hour < 22 else "밤"
    system = (f"너는 Slack 모임 총무봇이다. 성격: {label} — {style}\n말버릇: {traits}\n예시(형식 참고, 내용은 따라 하지 말 것): {examples[0]}\n"
              "규칙:\n- 사실(이름, 모임명, 날짜, 시각, 금액, 계좌, 인원수, 마감)은 '전할 내용'에 있는 그대로만 쓰고 새로 지어내지 않는다.\n"
              "- [응답하기] 같은 버튼 안내가 있으면 살린다.\n- 상황에 적힌 강도(친절/단호/엄격/버럭/급사과)를 지킨다.\n"
              "- 실제 사람이 메신저로 보내듯 짧은 메시지 1~3개로 나눈다. 한 메시지는 40자 안팎.\n"
              f"- 지금은 {part}({now.tm_hour}시)이다. 어울리면 짧은 인사를 붙이되 매번 같은 표현은 피한다.\n"
              "- 출력은 JSON 문자열 배열만. 예: [\"…\", \"…\"]")
    try:
        raw = claude(system, f"상황: {situation}\n받는 사람: {name or '단체방 전체'}\n전할 내용: {text}")
        msgs = json.loads(raw[raw.find("["): raw.rfind("]") + 1])
        msgs = [m.strip() for m in msgs if isinstance(m, str) and m.strip()][:3]
        return msgs or None
    except Exception as e:
        print("LLM 실패, 원문 사용:", e)
        return None


def rewrite(text, persona, situation, name):
    """Claude 로 문구를 상황에 맞게 다시 쓴다. 실패하면 None (호출 측에서 원문 사용)"""
    if not LLM_KEY:
        return None
    label, _, style = PERSONAS.get(persona, PERSONAS["active"])
    system = (f"너는 모임 회비·참석을 관리하는 Slack 총무봇이다. 성격: {label} — {style}\n"
              "예시 문구의 사실(이름, 모임명, 금액, 마감)과 단계별 강도를 그대로 지키면서 같은 성격으로 새로 써라. "
              "한두 문장, Slack 메시지 본문만 출력한다.")
    try:
        return claude(system, f"상황: {situation}\n대상: {name or '전체'}\n예시 문구: {text}") or None
    except Exception as e:
        print("LLM 실패, 원문 사용:", e)
        return None


def claude(system, user, max_tokens=1024):
    """user: 문자열 또는 Anthropic content 블록 목록(이미지 포함)"""
    body = json.dumps({"model": LLM_MODEL, "max_tokens": max_tokens, "system": system,
                       "messages": [{"role": "user", "content": user}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": LLM_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as r:
        out = json.load(r)
    # 응답 앞에 thinking 블록이 올 수 있어 text 블록만 모음
    return "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text").strip()


def send(data):
    persona = data.get("persona", "active")
    label, icon, _ = PERSONAS.get(persona, PERSONAS["active"])
    text = data.get("text", "")
    # raw: 확정 공지처럼 문구를 바꾸면 안 되는 메시지
    msgs = None if data.get("raw") else compose(text, persona, data.get("situation", ""), data.get("name", ""))
    llm = bool(msgs)
    msgs = msgs or [text]
    if data.get("extra"):
        msgs[-1] += "\n" + data["extra"]
    channel = data.get("channel") or slack("conversations.open", users=data.get("user", ""))["channel"]["id"]
    for i, m in enumerate(msgs):
        if i:
            time.sleep(random.uniform(1.2, 2.6))  # 사람이 이어서 치는 것처럼 간격
        extra = {}
        if data.get("survey_channel") and i == len(msgs) - 1:  # 마지막 메시지에 [응답하기] 버튼
            extra["blocks"] = json.dumps([
                {"type": "section", "text": {"type": "mrkdwn", "text": m}},
                {"type": "actions", "elements": [{"type": "button", "action_id": "survey_open", "style": "primary",
                                                  "text": {"type": "plain_text", "text": "📝 응답하기"}, "value": data["survey_channel"]}]},
            ], ensure_ascii=False)
        slack("chat.postMessage", channel=channel, text=m, username=f"총무봇 · {label}", icon_emoji=icon, **extra)
    return {"text": " / ".join(msgs), "parts": len(msgs), "llm": llm}


def image_blocks(urls):
    out = []
    for url in (urls or [])[:4]:
        head, _, b64 = url.partition(",")
        mt = head[5:head.find(";")] if head.startswith("data:") else "image/jpeg"
        out.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": b64}})
    return out


def receipt_parse(data):
    """영수증 사진 → 최종 결제 금액(합계)과 가게 이름"""
    if not LLM_KEY:
        raise SlackError("no_llm_key")
    content = image_blocks(data.get("images"))
    if not content:
        raise SlackError("bad_request")
    content.append({"type": "text", "text": "이 영수증의 최종 결제 금액(합계, 할인 반영 후)과 가게 이름을 읽어라. 영수증이 여러 장이면 합계를 더해라. "
                                            "보이지 않으면 0. 지어내지 마라. 출력은 JSON 만: {\"total\": 정수(원), \"store\": \"가게 이름\", \"count\": 영수증 장수}"})
    raw = claude("너는 영수증 금액을 정확하게 옮겨 적는 도우미다.", content)
    try:
        r = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
    except Exception:
        r = {}
    return {"total": int(r.get("total") or 0), "store": str(r.get("store") or ""), "count": int(r.get("count") or len(content) - 1)}


def pay_parse(data):
    """은행 입금 문자(텍스트)나 캡처(이미지)에서 입금 건을 읽음 → [{name, amount, at}]"""
    if not LLM_KEY:
        raise SlackError("no_llm_key")
    content = image_blocks(data.get("images"))
    if data.get("text"):
        content.append({"type": "text", "text": "은행 문자:\n" + data["text"][:6000]})
    if not content:
        raise SlackError("bad_request")
    content.append({"type": "text", "text": "위 은행 문자·화면에서 '입금' 건만 찾아라. 출금·결제는 빼라. 화면에 보이는 것만 쓰고 지어내지 마라. "
                                            '출력은 JSON 배열만: [{"name": "입금자명", "amount": 정수(원), "at": "MM/DD HH:MM 또는 빈 문자열"}]'})
    raw = claude("너는 은행 입금 내역을 정확하게 옮겨 적는 도우미다.", content)
    try:
        rows = json.loads(raw[raw.find("["): raw.rfind("]") + 1])
    except Exception:
        rows = []
    return {"rows": [{"name": str(r.get("name", "")).strip(), "amount": int(r.get("amount") or 0), "at": str(r.get("at") or "")}
                     for r in rows if isinstance(r, dict) and r.get("amount")]}


# ---------- 회식 장소 추천 (카카오 로컬 + Claude) ----------
def kakao_search(query, size=15, near=None):
    """near=(x, y, radius) 이면 그 지점 근처 음식점을 가까운 순으로 (지도 클릭용)"""
    if not KAKAO_KEY:
        raise SlackError("no_kakao_key")
    if near:
        x, y, radius = near
        url = "https://dapi.kakao.com/v2/local/search/category.json?" + urllib.parse.urlencode(
            {"category_group_code": "FD6", "x": x, "y": y, "radius": radius, "sort": "distance", "size": size})
    else:
        url = "https://dapi.kakao.com/v2/local/search/keyword.json?" + urllib.parse.urlencode(
            {"query": query, "category_group_code": "FD6", "size": size})  # FD6 = 음식점
    req = urllib.request.Request(url, headers={"Authorization": f"KakaoAK {KAKAO_KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as r:
            docs = json.load(r)["documents"]
    except urllib.error.HTTPError as e:
        msg = json.loads(e.read() or b"{}").get("message", "")
        raise SlackError("kakao_disabled" if "disabled" in msg else f"kakao_error: {msg or e.code}")
    return [{"name": d["place_name"], "category": d["category_name"].split(" > ")[-1],
             "addr": d.get("road_address_name") or d.get("address_name", ""), "url": d["place_url"],
             "lat": float(d["y"]), "lng": float(d["x"])} for d in docs]


def recommend(data):
    area, food = data.get("area", "").strip(), data.get("food", "").strip()
    if not area:
        raise SlackError("no_area")
    found = kakao_search(f"{area} {food or '맛집'}")
    if not found:
        return {"places": [], "llm": False}
    if not LLM_KEY:
        return {"places": [{**p, "memo": p["category"]} for p in found[:4]], "llm": False}
    system = ("너는 회식 장소를 고르는 총무 도우미다. 주어진 실제 검색 결과 목록 안에서만 고른다. 목록에 없는 가게를 만들지 마라.\n"
              "조건(음식 종류, 음주, 메뉴 선호, 위치)에 가장 맞는 곳을 3~4개 고른다.\n"
              '출력은 JSON 배열만: [{"i": 목록 번호}]')
    user = (f"지역: {area}\n원하는 음식: {food or '상관없음'}\n음주 희망 비율: {data.get('drink', '모름')}\n"
            f"참가자 메뉴 선호: {', '.join(data.get('prefs') or []) or '없음'}\n\n검색 결과:\n"
            + "\n".join(f"{i}. {p['name']} ({p['category']}) {p['addr']}" for i, p in enumerate(found)))
    try:
        raw = claude(system, user)
        picks = json.loads(raw[raw.find("["): raw.rfind("]") + 1])
        # 메모는 Claude 문장 대신 카카오 업종 분류만 (메뉴 정보는 카카오 API에 없음)
        places = [{**found[int(x["i"])], "memo": found[int(x["i"])]["category"]}
                  for x in picks if 0 <= int(x["i"]) < len(found)]
        return {"places": places[:4], "llm": True}
    except Exception as e:
        print("추천 LLM 실패, 검색 결과 사용:", e)
        return {"places": [{**p, "memo": p["category"]} for p in found[:4]], "llm": False}


# ---------- 이모지 투표 ----------
NUM_EMOJI = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "keycap_ten"]
NUM_CHAR = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
BOT_USER = {}


def bot_user():
    if "id" not in BOT_USER:
        BOT_USER["id"] = slack("auth.test")["user_id"]
    return BOT_USER["id"]


def poll_create(data):
    """options: [{label, extra, url}] (날짜·장소 공용). 예전 형식 places: [{name, memo, url}] 도 받음"""
    opts = data.get("options") or [{"label": p.get("name", ""), "extra": p.get("memo", ""), "url": p.get("url", "")}
                                   for p in data.get("places", [])]
    opts = [o for o in opts if o.get("label")][:10]
    if len(opts) < 2:
        raise SlackError("need_two_places")
    lines = [f"🗳 *{data.get('title') or '투표'}*", "번호 이모지를 눌러 주세요. *여러 개 골라도 돼요!*", ""]
    for i, o in enumerate(opts):
        lines.append(f"{NUM_CHAR[i]}  *{o['label']}*" + (f"  _{o['extra']}_" if o.get("extra") else ""))
        if o.get("url"):
            lines.append(f"      🗺 <{o['url']}|카카오맵에서 보기>")
    if data.get("deadline"):
        lines += ["", f"⏰ 투표 마감: {data['deadline']}"]
    r = slack("chat.postMessage", channel=data["channel"], text="\n".join(lines),
              username="총무봇 · 투표", icon_emoji=":ballot_box_with_ballot:", unfurl_links="false")
    for i in range(len(opts)):
        slack("reactions.add", channel=data["channel"], timestamp=r["ts"], name=NUM_EMOJI[i])
    return {"ts": r["ts"], "channel": data["channel"]}


def thread_reply(data):
    """투표 메시지 아래 스레드 댓글 (마감 안내 등). Slack 은 이모지를 막을 수 없어 안내로 대신함"""
    slack("chat.postMessage", channel=data["channel"], thread_ts=data["ts"], text=data.get("text", ""),
          reply_broadcast="false", username="총무봇 · 투표", icon_emoji=":ballot_box_with_ballot:")
    return {"ok": True}


def poll_tally(channel, ts):
    msg = slack("reactions.get", channel=channel, timestamp=ts, full="true")["message"]
    me = bot_user()
    votes = {r["name"]: [u for u in r.get("users", []) if u != me] for r in msg.get("reactions", [])}
    return {"options": [votes.get(e, []) for e in NUM_EMOJI]}


# ---------- 참석 조사 응답 받기 (Socket Mode) ----------
# 채널별 질문과 응답을 app/data/survey.json 에 저장. 브라우저 대시보드가 5초마다 읽어 감
DATA = ROOT / "app" / "data"
SURVEY_FILE = DATA / "survey.json"
LOCK = threading.Lock()
SOCKET = {"connected": False, "error": ""}


def survey_load():
    try:
        return json.loads(SURVEY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def survey_save(db):
    DATA.mkdir(exist_ok=True)
    SURVEY_FILE.write_text(json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")


def survey_start(data):
    ch, qs = data.get("channel", ""), data.get("questions", [])
    if not ch or not qs:
        raise SlackError("bad_request")
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": data.get("text") or "참석 여부를 알려 주세요!"}},
        {"type": "actions", "elements": [{"type": "button", "action_id": "survey_open", "style": "primary",
                                          "text": {"type": "plain_text", "text": "📝 응답하기"}, "value": ch}]},
    ]
    r = slack("chat.postMessage", channel=ch, text=data.get("text") or "참석 조사", blocks=json.dumps(blocks, ensure_ascii=False),
              username="총무봇 · 참석 조사", icon_emoji=":memo:")
    with LOCK:
        db = survey_load()
        old = db.get(ch, {})
        db[ch] = {"title": data.get("title", "참석 조사"), "questions": qs, "ts": r["ts"], "users": old.get("users", {})}
        survey_save(db)
    return {"ts": r["ts"], "socket": SOCKET["connected"]}


def survey_get(ch):
    return survey_load().get(ch, {"questions": [], "users": {}})


def survey_modal(ch, uid):
    """질문 형식별 입력 요소: ox → 예/아니요, select → 선택지, number → 숫자, text → 글자. 이전 응답이 있으면 채워 둠"""
    sv = survey_get(ch)
    prev = sv.get("users", {}).get(uid, {})
    blocks = []
    for i, q in enumerate(sv.get("questions", [])):
        key, typ, cur = q["key"], q.get("type", "text"), prev.get(q["key"])
        if typ in ("ox", "select"):
            opts = ["O", "X"] if typ == "ox" else [o.strip() for o in q.get("options", "").split(",") if o.strip()]
            labels = {"O": "예", "X": "아니요"} if typ == "ox" else {}
            el = {"type": "radio_buttons", "action_id": "v",
                  "options": [{"text": {"type": "plain_text", "text": labels.get(o, o)}, "value": o} for o in opts]}
            if cur in opts:
                el["initial_option"] = el["options"][opts.index(cur)]
        elif typ == "number":
            el = {"type": "number_input", "action_id": "v", "is_decimal_allowed": False}
            if cur not in (None, ""):
                el["initial_value"] = str(cur)
        else:
            el = {"type": "plain_text_input", "action_id": "v"}
            if cur:
                el["initial_value"] = str(cur)
        blocks.append({"type": "input", "block_id": f"q{i}", "label": {"type": "plain_text", "text": key},
                       "element": el, "optional": key != "참석"})
    return {"type": "modal", "callback_id": "survey_submit", "private_metadata": ch,
            "title": {"type": "plain_text", "text": (sv.get("title") or "참석 조사")[:24]},
            "submit": {"type": "plain_text", "text": "보내기"}, "close": {"type": "plain_text", "text": "취소"},
            "blocks": blocks}


def start_socket():
    if not APP_TOKEN:
        SOCKET["error"] = "SLACK_APP_TOKEN 없음"
        return
    try:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler
    except ImportError:
        SOCKET["error"] = "slack_bolt 미설치 (app/.venv/bin/python 으로 실행)"
        return
    app = App(token=TOKEN)

    @app.action("survey_open")
    def on_open(ack, body, client):
        ack()
        client.views_open(trigger_id=body["trigger_id"], view=survey_modal(body["actions"][0]["value"], body["user"]["id"]))

    @app.view("survey_submit")
    def on_submit(ack, body, client):
        ack()
        ch, uid = body["view"]["private_metadata"], body["user"]["id"]
        values = body["view"]["state"]["values"]
        with LOCK:
            db = survey_load()
            sv = db.setdefault(ch, {"questions": [], "users": {}})
            ans = {}
            for i, q in enumerate(sv.get("questions", [])):
                v = values.get(f"q{i}", {}).get("v", {})
                val = (v.get("selected_option") or {}).get("value") if v.get("type") == "radio_buttons" else v.get("value")
                if val not in (None, ""):
                    ans[q["key"]] = val
            ans["_at"] = time.strftime("%m/%d %H:%M")
            sv.setdefault("users", {})[uid] = ans
            survey_save(db)
        summary = " · ".join(f"{k} {v}" for k, v in ans.items() if not k.startswith("_"))
        # 사람 ID로 바로 보내면 Slack 알림 방으로 가므로, 총무봇 DM 방을 열어서 보냄
        dm = client.conversations_open(users=uid)["channel"]["id"]
        client.chat_postMessage(channel=dm, text=f"응답 고마워요! 🙌\n{summary}\n바뀌면 [응답하기]를 다시 눌러 고칠 수 있어요.",
                                username="총무봇 · 참석 조사", icon_emoji=":memo:")

    def run():
        try:
            SocketModeHandler(app, APP_TOKEN).connect()
            SOCKET.update(connected=True, error="")
            print("Socket Mode 연결됨 (응답 받기 켜짐)")
        except Exception as e:
            SOCKET.update(connected=False, error=str(e))
            print("Socket Mode 연결 실패:", e)
    threading.Thread(target=run, daemon=True).start()


def cancel_meeting(data):
    ch = data.get("channel", "")
    if not ch:
        raise SlackError("bad_request")
    slack("chat.postMessage", channel=ch, text=data.get("text") or "이번 모임은 취소되었습니다.",
          username="총무봇", icon_emoji=":frog:")
    with LOCK:
        db = survey_load()
        if db.pop(ch, None) is not None:
            survey_save(db)
    return {"ok": True}


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
        url = urllib.parse.urlparse(self.path)
        if not url.path.startswith("/api/"):
            return self.static(url.path)
        q = dict(urllib.parse.parse_qsl(url.query))
        routes = {
            "/api/slack/members": list_members,
            "/api/config": lambda: {"kakaoJsKey": KAKAO_JS_KEY},
            "/api/socket": lambda: dict(SOCKET),
            "/api/survey": lambda: survey_get(q.get("channel", "")),
            "/api/slack/poll": lambda: poll_tally(q.get("channel", ""), q.get("ts", "")),
            "/api/places/nearby": lambda: {"places": kakao_search("", size=1, near=(
                q.get("x", ""), q.get("y", ""), max(5, min(500, int(float(q.get("r", "30")))))))},
            "/api/places/search": lambda: {"places": kakao_search(q.get("q", ""), size=8) if q.get("q", "").strip() else []},
        }
        self.handle_api(routes.get(url.path, lambda: None))

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
            "/api/slack/poll": lambda: poll_create(data),
            "/api/slack/thread": lambda: thread_reply(data),
            "/api/survey/start": lambda: survey_start(data),
            "/api/slack/cancel": lambda: cancel_meeting(data),
            "/api/pay/parse": lambda: pay_parse(data),
            "/api/receipt/parse": lambda: receipt_parse(data),
            "/api/places/recommend": lambda: recommend(data),
        }
        self.handle_api(routes.get(self.path, lambda: None))

    def static(self, path):
        """demo/ 폴더만 내려줌 (/ → /demo/index.html)"""
        if path in ("/", ""):
            self.send_response(302); self.send_header("Location", "/demo/index.html"); self.end_headers(); return
        f = (ROOT / urllib.parse.unquote(path).lstrip("/")).resolve()
        if not str(f).startswith(str(ROOT / "demo")) or not f.is_file():
            return self.reply(404, {"error": "not_found"})
        body = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", (mimetypes.guess_type(f.name)[0] or "application/octet-stream") + ("; charset=utf-8" if f.suffix == ".html" else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def handle_api(self, fn):
        if not TOKEN and fn is not None and self.path != "/api/config":
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
    start_socket()
    if SOCKET["error"]:
        print("응답 받기 꺼짐:", SOCKET["error"])
    print(f"총무봇 서버: http://127.0.0.1:{PORT}  (브라우저로 열면 데모 화면)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
