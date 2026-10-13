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
  POST /api/slack/send     {channel|user, text, persona, situation, name, mood?, kind?, phase?, facts?, hint?, flavor?, parts?, survey_channel?}
                           Claude 가 성격대로 처음부터 짧은 메시지 1~3개로 써서 몇 초 간격으로 나눠 보냄
  POST /api/pay/parse      {text?, images?[dataURL]}  은행 입금 문자·캡처에서 입금자·금액·시각을 읽음
  POST /api/receipt/parse  {images[dataURL]}  영수증에서 최종 결제 금액과 가게 이름을 읽음
                           Claude 가 성격에 맞게 문구를 다시 쓰고(키 없거나 실패 시 text 그대로) 채널/DM 발송
  POST /api/places/recommend {area, food, prefs, drink}
                           카카오 로컬 검색 결과 중 Claude 가 조건에 맞는 후보를 골라 이유·예상 금액을 붙임
  GET  /api/places/search?q=  카카오맵 가게 검색 (직접 추가용 자동완성)
  GET  /api/places/nearby?x=&y=&r=  지도에서 클릭한 지점의 가장 가까운 음식점
  POST /api/slack/poll     {channel, title, options, deadline, persona?}  이모지 투표 메시지 + 번호 이모지 미리 달기
  POST /api/slack/thread   {channel, ts, text, persona?}  메시지 아래 스레드 댓글 (투표 마감 안내)
  POST /api/survey/start   {channel, title, text, questions, persona?}  [응답하기] 버튼이 달린 참석 조사 메시지
  GET  /api/survey?channel=  참석 조사 질문과 사람별 응답
  GET  /api/socket         응답 받기(Socket Mode) 연결 상태
  POST /api/slack/cancel   {channel, text, persona?}  모임 취소 안내, 참석 조사 기록 삭제 (채널은 그대로 둠)
  GET  /api/slack/poll?channel=&ts=  번호 이모지별 투표자 (봇 반응 제외)
  POST /api/meet           {channel, persona, info, names}  단체방 대화에 쓸 모임 정보·이름 (대시보드가 바뀔 때마다 보냄)
  GET  /api/chat?channel=  단체방 대화 처리 기록 (질문 대답·시트 반영·총무 확인 필요)
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
    "active": ("영크크", ":laughing:", "텐션 최고의 극외향 말투. 밝고 에너지 넘치며 느낌표와 이모지를 자주 쓴다."),
    "shy": ("구남친", ":pleading_face:", "헤어진 지 꽤 됐는데 아직 못 잊은 사람처럼, 새벽에 잠 못 들고 머뭇거리며 연락한다. "
            "겉으로는 안부·추억·핑계를 대지만 진짜 용건은 언제나 참석 여부와 회비 입금이다."),
    "oldie": ("부장님", ":older_man:", "50대 초반 부장님. 푸근하고 정이 많지만 잔소리가 많다. 존댓말과 반말을 섞어 쓴다. 화나면 영화 '타짜'의 곽철용으로 돌변한다."),
    "strict": ("맑눈광", ":face_with_monocle:", "원칙과 기한을 중시하는 공식적인 말투. 정중하지만 단호하고 예외를 두지 않는다. 비꼬기·모욕은 하지 않는다."),
}
# 그림 아이콘을 쓰는 성격: Slack 은 공개 주소의 그림만 쓸 수 있어 GitHub Pages 에 올라간 파일을 씀.
# 그 주소가 아직 안 열리면(올리기 전) 위의 이모지 아이콘으로 보냄
PERSONA_IMG = {"oldie": "assets/bujang.jpg", "shy": "assets/exbf.jpg", "strict": "assets/mnk.jpg"}
ASSET_BASE = os.environ.get("PUBLIC_ASSET_BASE", "https://greatdh84.github.io/chongmubot/demo/")
_IMG_OK = {}  # url → (열리는지, 확인 시각)


def persona_icon(persona, emoji):
    """chat.postMessage 아이콘 인자: 그림이 공개돼 있으면 icon_url, 아니면 icon_emoji"""
    path = PERSONA_IMG.get(persona)
    if path:
        url = ASSET_BASE + path
        ok, at = _IMG_OK.get(url, (False, 0))
        if time.time() - at > 600:  # 10분마다 다시 확인
            try:
                req = urllib.request.Request(url, method="HEAD")
                with urllib.request.urlopen(req, timeout=5, context=SSL_CTX) as r:
                    ok = r.status == 200 and r.headers.get("Content-Type", "").startswith("image/")
            except Exception:
                ok = False
            _IMG_OK[url] = (ok, time.time())
        if ok:
            return {"icon_url": url}
    return {"icon_emoji": emoji}


# 성격별 말버릇과 예시 대화 (처음부터 쓸 때 말투가 일정하게)
TRAITS = {
    "active": ("'~요!', '~용', 'ㅎㅎ', 'ㅋㅋ'를 섞고 이모지를 메시지마다 1~3개. 느낌표 많이, 텐션 최고. 반말은 안 씀.",
               ['["도형님~ 좋은 아침이에요! ☀️", "회식 장소 투표 아직이시죠? ㅎㅎ", "이모지 하나만 콕! 오늘 6시 마감이에용 🙌"]']),
    "shy": ("'○○님' + 존댓말. 문장을 끝맺지 못하고 흐린다. 마침표 대신 '…'이나 줄바꿈으로 짧은 줄을 여러 번 끊는다. "
            "'갑자기', '그냥', '문득', '혹시', '잠이 안 와서'로 연락 이유를 얼버무린다. 이모지는 거의 안 쓰고 써도 'ㅎ', 'ㅋㅋ' 정도. "
            "'술 좀 마셨어요', '잠이 안 와서요' 같은 새벽 핑계를 쓴다. 가끔 오타를 하나 섞어 살짝 취한 느낌을 낸다(예: 회비→회뷔, 입금→입긍). "
            "한 메시지는 3~4줄 이내. 모임명·날짜·금액·계좌·마감일은 정확하게 쓰고 여기엔 오타를 넣지 않는다.",
            ['["자요?\\n갑자기 생각나서요\\n10월 팀 회식… 오실 수 있어요?"]']),
    "oldie": ("존댓말과 반말을 섞음('~해요', '~했지?', '~해라', '~하자고'). 개인 DM에선 상대를 '○○씨'라고 부르고 반말 비중이 높고, "
              "단체방에선 '여러분~'이라고 부르고 존댓말 비중이 높음. '허허', '^^', '~~', '..'를 즐겨 쓰고 이모지는 거의 안 씀. "
              "전할 내용에 '○○님'이 있으면 '○○씨'로 바꿔 부름. 욕설·인신공격·실제 위협은 하지 않음.",
              ['["허허 도형씨 좋은 아침~^^", "회식 장소 투표 아직이지? 이모지 하나만 꾹 눌러 줘요~~", "오늘 6시 마감이야.."]']),
    "strict": ("'~하십시오', '~바랍니다'로 끝나는 공식적인 존댓말. 기한·원칙을 분명히 말하고 이모지는 쓰지 않음. 비꼬기·모욕 금지.",
               ['["도형님, 참석 조사 회신이 확인되지 않았습니다.", "오늘 16시까지 [응답하기]로 회신해 주십시오.", "기한을 지켜 주시기 바랍니다."]']),
}


# 부장님(50대 초반) 강도별 말투와 예시. 버럭은 곽철용 모드
OLDIE_MOODS = {
    "soft": ("푸근하고 다정한 부장님. 존댓말 위주에 반말을 살짝 섞는다.",
             '["허허 도형씨~ 잘 지내지?^^", "참석 조사 아직이던데, [응답하기] 한 번만 눌러 줘요~~", "오늘 4시 마감이야.."]'),
    "firm": ("잔소리하는 부장님. 반말 비중이 늘고('~해라', '~하자고') 웃음기는 조금만 남긴다.",
             '["도형씨, 투표 아직이네~", "이모지 하나 누르는 거 금방이잖아. 오늘 6시까지 해라~"]'),
    "last": ("웃음기를 뺀 진지한 부장님. 짧고 묵직하게. '허허', '^^', '~~'는 쓰지 않는다.",
             '["도형씨.", "오늘 6시 마감이다.", "안 하면 다수결로 간다."]'),
    "burst": ("영화 '타짜'의 곽철용으로 돌변한다. 아래 '이번 대사'를 살려서 상황(투표·참석 응답·입금)에 맞게 단어만 바꾸고, "
              "전할 사실의 핵심(마감 등)도 한 번은 넣는다. 개인 DM이면 '너희들·니네'를 상대 이름으로 바꾼다. "
              "단체방이어도 '여러분~', 존댓말, '허허', '^^', '~~'는 쓰지 않고 끝까지 곽철용의 낮고 무거운 반말로 말한다. "
              "대사 뒤에는 마감 같은 핵심 사실 한 가지만 짧게 붙이고 길게 늘어놓지 않는다. "
              "욕설·인신공격·실제 위협은 하지 않고 영화 대사 패러디 수준에서 멈춘다.",
              '["나 깡패 아니다.", "나도 적금 붓고 보험 들고 살고 그런다. 나도 순정이 있다.", "도형씨가 이런 식으로 투표 안 하고 그러면.. 그때는 깡패가 되는 거야! 오늘 6시까지다."]'),
    "sorry": ("버럭한 뒤 부장님으로 돌아와 멋쩍게 사과한다. 짧게. 영화나 캐릭터 이름은 꺼내지 않는다.",
              '["허허.. 내가 좀 흥분했네~", "미안미안^^ 편할 때 눌러 줘요~~"]'),
}
# 구남친(새벽 연락) 강도별 레퍼토리 묶음. 서버가 매번 몇 개만 골라 예시로 줌 (받는 사람별로 최근에 쓴 건 빼고)
EXBF_MOODS = {
    "soft": "새벽 연락형(첫 연락·참석 확인) 또는 안부 위장형(회비·정산 요청)",
    "firm": "추억 소환형 / 핑계형 / 미련·후회형 중 하나",
    "last": "읽씹 후 연타형. '자요?'를 단독으로 반복해 시작해도 된다. 마감일을 꼭 넣는다",
    "burst": "읽씹 후 연타형을 더 짧게, 한두 단어씩 여러 번 끊어서. 마지막에 '…근데 ○○은 잊지 말아주세요'처럼 용건",
    "sorry": "'미안해요 그냥 잊어주세요', 'ㅋㅋ 제가 왜 이러죠'처럼 물러서는 척하다가 용건 한 줄",
}
EXBF_LINES = {
    "soft": ["자요?\n갑자기 생각나서요\n{날짜} {모임명}… 오실 수 있어요?", "자요?\n뭐 해요\n그냥… {모임명} 오시나 해서",
             "자요?\n술 좀 마셨어요\n근데 {모임명}은 진심으로 물어보는 거예요", "아 실수로 보냈네요…\n근데 온 김에 참석 여부만…",
             "자요?\n그냥 잘 지내나 궁금해서요\n아 그리고… {금액}원이요", "별일 없죠?\n지난 {모임명} 정산이요…\n{은행} {계좌번호} ({예금주})"],
    "firm": ["자요?\n오늘 {장소} 지나가다가… {이름}님 생각났어요", "우리 지난번 갔던 거기…\n이번에도 거기예요",
             "그때 {이름}님이 시켰던 거…\n이번에도 드실 거예요?", "자요?\n{이름}님 그 영수증… 아직 저한테 있어요",
             "잠이 안 와서요…\n혹시 그때 그 식당 이름 기억나요?\n아 그리고 회비…", "계좌번호 안 바꿨어요…\n그대로예요. {은행} {계좌번호}",
             "자요?\n제가 지난번에 너무 급하게 물어봤던 것 같아요…", "저 많이 변했어요…\n이번엔 정산 금방 끝내요",
             "요즘 {이름}님처럼 제때 입금해주는 사람이 없더라고요…", "{이름}님만한 사람 없더라고요…\n{금액}원만요"],
    "last": ["자요?", "자요…?", "바빠요?", "답장 안 해도 돼요…", "…", "{마감일}까지만 기다릴게요"],
    "burst": ["자요?", "자요…?", "바빠요?", "…", "미안해요 그냥 잊어주세요", "…근데 {금액}원은 잊지 말아주세요"],
    "sorry": ["미안해요 그냥 잊어주세요", "ㅋㅋ 제가 왜 이러죠", "…근데 {금액}원은 잊지 말아주세요"],
}
EXBF_DONE = ["확인했어요… 고마워요\n이제 자도 되겠네요 ㅎ", "이제 연락 안 할게요", "…다음 모임 때 또 연락해도 돼요?"]
# 받는 사람별 '오늘 첫 연락' 날짜와 최근에 보낸 문장 (구남친: 첫 메시지는 '자요?', 같은 문장 반복 금지). 서버를 끄면 사라짐
EXBF_DAY, EXBF_SENT = {}, {}


def exbf_extra(key, mood, group, name):
    today = time.strftime("%Y-%m-%d")
    with RECENT_LOCK:
        first = EXBF_DAY.get(key) != today
        sent = list(EXBF_SENT.get(key, []))
    pool = EXBF_LINES.get(mood, EXBF_LINES["soft"])
    picks = random.sample(pool, min(len(pool), 3))
    out = (f"이번 레퍼토리: {EXBF_MOODS.get(mood, EXBF_MOODS['soft'])}\n"
           f"참고 대사(골라서 상황에 맞게 변형, 그대로 베끼지 말 것): {json.dumps(picks, ensure_ascii=False)}\n")
    if first:
        out += f"- 오늘 이 {'단체방' if group else '사람'}에게 보내는 첫 메시지다. 첫 메시지는 이름 없이 반드시 '자요?'로 시작한다(오타 '쟈요?', '자여?'로 변주 가능). 이름은 '자요?' 앞에 붙이지 않고, 용건은 다음 줄에 돌려 말한다.\n"
    else:
        out += "- 오늘 이미 연락했다. '자요?'로 다시 시작해도 되지만 다른 방식(안부·추억·핑계)도 좋다.\n"
    if group:
        out += "- 단체방이면 특정인 이름 대신 '다들'·'여러분'으로 부르고, 인원수만 말한다.\n"
    if sent:
        out += f"- 이 {'단체방' if group else '사람'}에게 이미 보낸 문장(반복 금지, '자요?'만 예외): {json.dumps(sent[-8:], ensure_ascii=False)}\n"
    return out


# 성격별 추가 모드 (화면의 토글): 구남친 '감성 ON', 부장님 '라떼모드 ON'
EMO_EXTRA = ("★감성 ON (가장 중요): 새벽 감성이 폭발한 상태다. 레퍼토리보다 이걸 우선한다. 반드시 ① 같이 갔던 곳·그날 날씨·같이 듣던 노래 같은 추억을 한 줄, "
             "② 달·비·별·가로등 같은 비유를 한 줄 넣는다. 발라드 가사처럼 아련하게 쓰고 🌙·🥀 이모지를 하나 쓴다. "
             "메시지는 4~5개로 나눠도 된다. 그래도 전할 사실(마감 등)은 마지막에 꼭 넣는다.\n")
LATTE_TOPICS = ["삐삐로 약속 잡던 시절", "회식은 무조건 3차까지 가던 시절", "출석부에 볼펜으로 이름 쓰던 시절", "지하철 토큰 쓰던 시절",
                "노래방 탬버린 담당이던 시절", "공중전화 앞에 줄 서던 시절", "카세트테이프로 노래 녹음하던 시절", "막내가 고기 굽던 시절",
                "엑셀 대신 장부에 손으로 회비 적던 시절", "다이얼 전화로 회식 예약하던 시절"]


def latte_extra(key):
    return (f"라떼모드 ON: 메시지마다 '라떼는 말이야~' 또는 '나 때는~'으로 시작하는 옛날 얘기를 한 줄 꼭 넣는다. "
            f"이번 소재: {pick_fresh(key + ':latte', LATTE_TOPICS)}. 옛날 얘기 뒤에 전할 사실로 자연스럽게 돌아온다.\n")


def exbf_record(key, msgs):
    with RECENT_LOCK:
        EXBF_DAY[key] = time.strftime("%Y-%m-%d")
        lst = EXBF_SENT.setdefault(key, [])
        lst.extend(m for m in msgs if m.strip() not in ("자요?", "자요…?"))
        del lst[:-12]


# 곽철용 대사: 버럭 때 서버가 하나를 골라 줌 (받는 사람별로 최근에 쓴 건 빼고)
KWAK_LINES = [
    "나 깡패 아니다. 나도 적금 붓고 보험 들고 살고 그런다. 나도 순정이 있다. 너희들이 이런 식으로 답 안 하고 그러면 그때는 깡패가 되는 거야!",
    "니네하고 나하고 아주 전생에 인연이 깊구나. 이 바닥 좁다. 대답 좀 해라.",
    "어이 젊은 친구, 신사답게 대답해.",
    "시흥대교는 무너졌냐. 왜 아무 소식이 없어!",
]
# 예시 문구 단계 키 → 강도 (화면의 LINES 는 g·dm0·dl0·dm1·dl2 를 씀)
MOOD_OF = {"g": "soft", "dm0": "soft", "dl0": "soft", "dm1": "firm", "dl2": "last"}
# 부장님 유행어: (문구, 조건). 조건이 맞는 것 중 절반 확률로 하나만 골라 Claude 에게 줌 (Claude 가 고르면 매번 같은 것만 씀)
OLDIE_PHRASES = [
    ("뭘 먹어야 맛있게 먹었다고 소문이 날까~", lambda c: c["kind"] == "dinner" and c["phase"] in ("plan", "survey") and c["mood"] == "soft"),
    ("술 먹고 내일 지각하면 안돼~~ 안 봐도 비디오야~", lambda c: c["phase"] in ("plan", "survey", "event") and c["mood"] in ("soft", "firm")),
    ("{who} 시계 밥 좀 줘라", lambda c: c["mood"] in ("firm", "last")),
    ("지방방송 끄세요~~", lambda c: c["group"] and c["mood"] in ("soft", "firm")),
    ("회식 끝나고 하드 하나 먹을까~~?", lambda c: c["kind"] == "dinner" and c["phase"] in ("plan", "survey", "event") and c["mood"] in ("soft", "sorry")),
    ("추우니 돔바 하나 걸치고 나오세요", lambda c: c["month"] in (11, 12, 1, 2, 3) and c["phase"] in ("plan", "survey", "event") and c["mood"] == "soft"),
    ("나 때는 여기 다 논밭이었어", lambda c: c["mood"] == "soft"),
    ("자자자~ 주목!", lambda c: c["group"] and c["mood"] in ("soft", "firm")),
    ("이거 다 여러분 위해서 하는 거야~", lambda c: c["mood"] == "firm"),
    ("바쁘다 바빠 현대사회~", lambda c: c["mood"] in ("soft", "firm", "sorry")),
    ("일단 고! 생각은 나중에~", lambda c: c["phase"] in ("plan", "survey") and c["mood"] in ("soft", "firm")),
    ("막내는 고기 굽는 거 아니다, 다 같이 굽자~", lambda c: c["kind"] == "dinner" and c["phase"] in ("plan", "survey", "event") and c["mood"] == "soft"),
    ("집에 갈 땐 조심히들 들어가고~", lambda c: c["phase"] in ("survey", "event") and c["mood"] == "soft"),
]
# 부장님 잡담 소재: 친절·단호·급사과일 때 하나를 줘서 어울리면 한 줄 (문장이 매번 달라지게)
OLDIE_TOPICS = ["주말 골프", "등산", "건강검진 결과", "주식", "트로트", "라떼는~", "사모님", "다이어트", "허리 디스크", "아침 조기축구"]
# 받는 사람(채널·사용자)별 최근에 쓴 유행어·대사·소재. 서버를 끄면 사라짐
RECENT = {}
RECENT_LOCK = threading.Lock()


def pick_fresh(key, cands):
    """후보 중 이 받는 사람에게 최근 5번 안에 안 쓴 것을 하나 골라 기록. 모두 썼으면 아무거나"""
    with RECENT_LOCK:
        used = RECENT.setdefault(key, [])
        fresh = [c for c in cands if c not in used] or cands
        choice = random.choice(fresh)
        used.append(choice)
        del used[:-5]
    return choice


def oldie_phrase(key, mood, kind, phase, group, name):
    c = {"mood": mood, "kind": kind, "phase": phase, "group": group, "month": time.localtime().tm_mon}
    cands = [t for t, ok in OLDIE_PHRASES if ok(c)]
    if not cands or random.random() < 0.5:
        return ""
    return pick_fresh(key, cands).replace("{who}", "다들" if group or not name else f"{name}씨,")


def hint_text(hint, group, mood):
    """화면이 보낸 상황 정보(마감 시각·몇 번째 연락·남은 인원)를 Claude 가 쓸 수 있는 문장으로"""
    hint = hint or {}
    out = []
    now = time.localtime()
    out.append(f"오늘은 {'월화수목금토일'[now.tm_wday]}요일")
    try:
        dl = time.mktime(time.strptime(hint["deadline_at"][:16], "%Y-%m-%dT%H:%M"))
        left = int((dl - time.time()) // 60)
        out.append(f"마감까지 {left // 1440}일 남음" if left >= 1440 else f"마감까지 {left // 60}시간 {left % 60}분 남음" if left >= 60
                   else f"마감까지 {left}분 남음" if left >= 0 else "마감이 이미 지남")
    except (KeyError, TypeError, ValueError):
        pass
    if hint.get("nth"):
        out.append(f"이 {'단체방' if group else '사람'}에게 {hint['nth']}번째 연락")
    # 다 주면 전부 나열하므로 버럭·엄격은 1개, 나머지는 1~2개만 무작위로
    return " · ".join(random.sample(out, min(len(out), 1 if mood in ("last", "burst") else random.choice((1, 2)))))


MSGS_SCHEMA = {"type": "object", "properties": {"messages": {"type": "array", "items": {"type": "string"}}},
               "required": ["messages"], "additionalProperties": False}


def compose(text, persona, situation, name, mood="", kind="", phase="", group=False, facts="", hint=None, key="", flavor=None):
    """전할 내용(사실)을 바탕으로 Claude 가 성격대로 짧은 메시지 1~3개를 처음부터 씀. 실패하면 None
    facts 가 있으면(부장님) 완성 문장 대신 사실만 넘겨 매번 새로 쓰게 함. key 는 반복 막기용 받는 사람"""
    if not LLM_KEY:
        return None
    label, _, style = PERSONAS.get(persona, PERSONAS["active"])
    traits, examples = TRAITS.get(persona, TRAITS["active"])
    mood = MOOD_OF.get(mood, mood if mood in OLDIE_MOODS else "soft")
    now = time.localtime()
    part = "아침" if 5 <= now.tm_hour < 11 else "점심" if now.tm_hour < 14 else "오후" if now.tm_hour < 18 else "저녁" if now.tm_hour < 22 else "밤"
    extra, length, body = "", "40자 안팎", f"전할 내용: {text}"
    if persona == "oldie":
        how, ex = OLDIE_MOODS[mood]
        examples = [ex]
        extra = f"이번 말투: {how}\n"
        if mood == "burst":
            extra += f"이번 대사(꼭 살릴 것): {pick_fresh(key + ':kwak', KWAK_LINES)}\n"
            length = "70자 안팎(곽철용 대사는 중간에 끊지 않음)"
        phrase = oldie_phrase(key, mood, kind, phase, group, name)
        if phrase:
            extra += f"이번에 넣을 말버릇(어색하지 않으면 꼭 한 번 그대로 넣기): {phrase}\n"
        if mood in ("soft", "firm", "sorry"):
            extra += (f"잡담 소재(가능하면 반 줄로 가볍게 한 번. 예: '주말에 골프 쳤더니 허리가 아주..'): {pick_fresh(key + ':topic', OLDIE_TOPICS)}\n"
                      "'허허', '^^', '~~', 반말 한두 마디 중 두 가지 이상은 꼭 드러나게 한다. 전할 사실의 딱딱한 문체(~십시오, ~입니다)는 따라 하지 않는다.\n")
        if facts:
            body = (f"전할 사실(이 문장은 말투를 무시하고 사실만 뽑아 쓸 것): {facts}\n"
                    f"참고 정보(어울리면 한 번만 자연스럽게, 아니면 빼기): {hint_text(hint, group, mood)}")
        if (flavor or {}).get("latte") and mood != "burst":  # 곽철용 버럭에는 안 섞음
            extra += latte_extra(key)
    if persona == "shy":
        extra = exbf_extra(key, mood, group, name) + (EMO_EXTRA if (flavor or {}).get("emo") else "")
        length = "3~4줄 이내(줄바꿈으로 짧게 끊기)"
        if facts:
            body = f"전할 사실(말투는 무시하고 사실만 뽑아 쓸 것): {facts}"
    system = (f"너는 Slack 모임 총무봇이다. 성격: {label} — {style}\n말버릇: {traits}\n{extra}예시(형식 참고, 내용은 따라 하지 말 것): {examples[0]}\n"
              "규칙:\n- 사실(이름, 모임명, 날짜, 시각, 금액, 계좌, 인원수, 마감)은 받은 내용에 있는 그대로만 쓰고 새로 지어내지 않는다.\n"
              "- [응답하기] 같은 버튼 안내가 있으면 살린다.\n"
              "- 사람 수를 '분'으로 셀 때는 숫자 대신 고유어로 쓴다(1분→한분, 2분→두분, 3분→세분, 4분→네분). '3분'처럼 쓰면 시간으로 읽힌다.\n"
              f"- 강도 {mood}(soft=친절, firm=단호, last=엄격, burst=버럭, sorry=급사과)를 지킨다. 상황·강도를 설명하는 단어(예: '단호하게')는 본문에 쓰지 않는다.\n"
              f"- 실제 사람이 메신저로 보내듯 짧은 메시지 {'1~5' if persona == 'shy' and mood in ('last', 'burst') else '1~3'}개로 나눈다. 한 메시지는 {length}.\n"
              + ("- 지금은 새벽(잠 못 드는 시간)이라는 설정으로 쓴다. 인사는 붙이지 않는다.\n" if persona == "shy"
                 else f"- 지금은 {part}({now.tm_hour}시)이다. 어울리면 짧은 인사를 붙이되 매번 같은 표현은 피한다.\n" if mood not in ("last", "burst")
                 else "- 인사·안부는 붙이지 않고 바로 본론으로 들어간다.\n") +
              "- 나눠 보낼 메시지들을 messages 배열에 순서대로 담는다.")
    where = "단체방 전체" if group or not name else name
    try:
        # 구조화 출력 + 넉넉한 max_tokens (생각하느라 토큰을 다 써서 빈 대답이 오는 일 방지)
        raw = claude(system, f"상황: {situation}\n받는 사람: {where}\n{body}", max_tokens=4000, schema=MSGS_SCHEMA)
        msgs = json.loads(raw)["messages"]
        msgs = [m.strip() for m in msgs if isinstance(m, str) and m.strip()][:5 if persona == "shy" else 3]
        if msgs and persona == "shy":
            exbf_record(key, msgs)
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


def claude(system, user, max_tokens=1024, schema=None):
    """user: 문자열 또는 Anthropic content 블록 목록(이미지 포함). schema 를 주면 그 JSON 형식으로만 답함(구조화 출력)"""
    req_body = {"model": LLM_MODEL, "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": user}]}
    if schema:
        req_body["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
    body = json.dumps(req_body).encode()
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
    msgs = None if data.get("raw") else compose(text, persona, data.get("situation", ""), data.get("name", ""),
                                                data.get("mood", ""), data.get("kind", ""), data.get("phase", ""), bool(data.get("channel")),
                                                data.get("facts", ""), data.get("hint"), data.get("channel") or data.get("user", ""),
                                                data.get("flavor"))
    llm = bool(msgs)
    # parts: 문구 그대로 여러 개로 나눠 보냄 (구남친 '읽씹 후 연타' 같은 멘트)
    msgs = msgs or ([p for p in data.get("parts") or [] if p] if data.get("raw") else []) or [text]
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
        slack("chat.postMessage", channel=channel, text=m, username=f"총무봇 · {label}", **persona_icon(persona, icon), **extra)
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


def sender(persona, fallback_name, fallback_emoji):
    """투표·참석 조사·취소 안내도 성격 이름과 아이콘(부장님은 사진)으로. persona 가 없으면 예전 이름"""
    if persona in PERSONAS:
        label, icon, _ = PERSONAS[persona]
        return {"username": f"총무봇 · {label}", **persona_icon(persona, icon)}
    return {"username": fallback_name, "icon_emoji": fallback_emoji}


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
              unfurl_links="false", **sender(data.get("persona"), "총무봇 · 투표", ":ballot_box_with_ballot:"))
    for i in range(len(opts)):
        slack("reactions.add", channel=data["channel"], timestamp=r["ts"], name=NUM_EMOJI[i])
    return {"ts": r["ts"], "channel": data["channel"]}


def thread_reply(data):
    """투표 메시지 아래 스레드 댓글 (마감 안내 등). Slack 은 이모지를 막을 수 없어 안내로 대신함"""
    slack("chat.postMessage", channel=data["channel"], thread_ts=data["ts"], text=data.get("text", ""),
          reply_broadcast="false", **sender(data.get("persona"), "총무봇 · 투표", ":ballot_box_with_ballot:"))
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
              **sender(data.get("persona"), "총무봇 · 참석 조사", ":memo:"))
    with LOCK:
        db = survey_load()
        old = db.get(ch, {})
        db[ch] = {"title": data.get("title", "참석 조사"), "questions": qs, "ts": r["ts"], "users": old.get("users", {}),
                  "persona": data.get("persona", "")}  # 응답 고맙다는 DM 도 같은 성격으로
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
            persona = sv.get("persona", "")
        summary = " · ".join(f"{k} {v}" for k, v in ans.items() if not k.startswith("_"))
        # 사람 ID로 바로 보내면 Slack 알림 방으로 가므로, 총무봇 DM 방을 열어서 보냄
        dm = client.conversations_open(users=uid)["channel"]["id"]
        text = (f"{random.choice(EXBF_DONE)}\n{summary}" if persona == "shy"
                else f"응답 고마워요! 🙌\n{summary}\n바뀌면 [응답하기]를 다시 눌러 고칠 수 있어요.")
        client.chat_postMessage(channel=dm, text=text, **sender(persona, "총무봇 · 참석 조사", ":memo:"))

    @app.event("message")
    def on_message(event, client):
        # 사람이 단체방에 쓴 새 메시지만 (수정·입장 알림·봇 메시지·DM 은 제외)
        if event.get("subtype") or event.get("bot_id") or event.get("channel_type") == "im" or event.get("user") == bot_user():
            return
        chat_handle(event.get("channel", ""), event.get("user", ""), event.get("text", ""), event.get("ts"), event.get("thread_ts"), client)

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
          **sender(data.get("persona"), "총무봇", ":frog:"))
    with LOCK:
        db = survey_load()
        if db.pop(ch, None) is not None:
            survey_save(db)
    return {"ok": True}


# ---------- 단체방 대화 (질문 대답 · 자연어로 시트 갱신) ----------
# 대시보드가 모임 정보를 /api/meet 로 보내 두면, 그 채널의 메시지를 Claude 가 보고
# 질문엔 성격대로 대답하고, 본인 정보 변경("2차는 빠질게요")은 참석 조사 응답(survey.json)에 바로 반영.
# 대시보드는 5초마다 응답을 읽어 시트에 반영하고, /api/chat 으로 대화 기록을 가져감
MEET_FILE = DATA / "meet.json"
CHAT = {}        # 채널 → 최근 대화 처리 기록 (서버를 끄면 사라짐)
LAST_SEEN = {}   # (채널, 사람) → 마지막 처리 시각 (연달아 말할 때 속도 제한)
HISTORY = {}     # 채널 → 최근 대화 [(이름, 말)] (봇 대답 포함, 잡담이 이어지게)
STREAK = {}      # 채널 → 잡담이 연달아 오간 횟수 (2번 넘으면 본론으로 끌고 옴)


def meet_load():
    try:
        return json.loads(MEET_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def meet_save(data):
    """{channel, persona, info:{...}, names:{slackId: 부를 이름}, todo:{slackId: 아직 안 한 일}}"""
    ch = data.get("channel", "")
    if not ch:
        raise SlackError("bad_request")
    with LOCK:
        db = meet_load()
        db[ch] = {"persona": data.get("persona", "active"), "info": data.get("info", {}), "names": data.get("names", {}),
                  "flavor": data.get("flavor", {}), "ended": bool(data.get("ended")),
                  "todo": data.get("todo", {}),
                  "at": time.strftime("%m/%d %H:%M")}
        DATA.mkdir(exist_ok=True)
        MEET_FILE.write_text(json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True}


def chat_events(ch):
    return {"events": CHAT.get(ch, [])}


def valid_value(q, v):
    """조사 항목 형식에 맞는 값만 통과. 안 맞으면 None"""
    v = str(v).strip()
    typ = q.get("type", "text")
    if typ == "ox":
        return {"O": "O", "X": "X", "예": "O", "아니요": "X"}.get(v.upper() if len(v) == 1 else v)
    if typ == "select":
        opts = [o.strip() for o in q.get("options", "").split(",") if o.strip()]
        return v if v in opts else None
    if typ == "number":
        return v if v.isdigit() else None
    return v[:40] or None


CHAT_SCHEMA = {  # 구조화 출력: 따옴표가 섞여도 항상 올바른 JSON 으로 받음
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["question", "update", "clarify", "other", "chat", "ignore"]},
        "updates": {"type": "array", "items": {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
                                               "required": ["key", "value"], "additionalProperties": False}},
        "reply": {"type": "string"},
        "owner_note": {"type": "string"},
    },
    "required": ["kind", "updates", "reply", "owner_note"],
    "additionalProperties": False,
}


def chat_think(meet, sv, uid, name, text, history, streak):
    """Claude 에게 최근 대화와 새 메시지를 보여 주고 {kind, updates, reply, owner_note} 를 받음"""
    persona = meet.get("persona", "active")
    label, _, style = PERSONAS.get(persona, PERSONAS["active"])
    traits, examples = TRAITS.get(persona, TRAITS["active"])
    tone = f"\n평소 말투: {OLDIE_MOODS['soft'][0]}" if persona == "oldie" else ""
    flavor = meet.get("flavor") or {}
    if persona == "oldie" and flavor.get("latte"):
        tone += "\n" + latte_extra(meet.get("_ch", "") + uid).strip()
    if persona == "shy" and flavor.get("emo"):
        tone += "\n" + EMO_EXTRA.strip()
    qs = sv.get("questions", []) if sv else []
    fields = [f"{q['key']}({q.get('type')}: {'O 또는 X' if q.get('type') == 'ox' else q.get('options') or '자유 입력'})" for q in qs]
    prev = (sv or {}).get("users", {}).get(uid, {})
    steer = ("이번엔 본론으로 끌고 온다: 맞장구 한마디 뒤에 말한 사람이 아직 안 한 일을 권하거나(있으면), 모임 날짜·장소로 마무리한다."
             if streak >= 2 else "아직은 본론을 서두르지 말고 성격대로 잡담을 받아 주고, 대화가 이어지게 가볍게 되물어도 된다.")
    system = (
        f"너는 Slack 모임 단체방에 같이 있는 총무봇이다. 단체방 사람들과 수다도 떠는 분위기 메이커다. 성격: {label} — {style}\n말버릇: {traits}{tone}\n"
        f"말투 예시(형식만 참고): {examples[0]}\n\n"
        f"오늘은 {time.strftime('%m/%d')}({'월화수목금토일'[time.localtime().tm_wday]}). 날짜는 모임 정보에 적힌 그대로 쓰고 '오늘·내일'로 바꿔 말하지 않는다.\n"
        f"모임 정보(이것만 사실로 쓴다): {json.dumps(meet.get('info', {}), ensure_ascii=False)}\n"
        f"참석 조사 항목: {', '.join(fields) if fields else '(아직 참석 조사 전이라 기록할 항목 없음)'}\n"
        f"말한 사람의 지금 기록: {json.dumps({k: v for k, v in prev.items() if not k.startswith('_')}, ensure_ascii=False) or '없음'}\n"
        f"말한 사람이 아직 안 한 일: {meet.get('todo', {}).get(uid) or '없음'}\n\n"
        "단체방의 새 메시지 하나를 보고 아래 중 하나로 판단한다. 웬만한 말에는 끼어든다.\n"
        "- question: 모임에 대한 질문(장소·시간·회비·마감·다른 후보 등). 모임 정보로 답한다. 정보에 없으면 지어내지 말고 총무에게 물어보겠다고 답하고 owner_note 에 질문을 적는다.\n"
        "- update: 말한 사람 '본인'의 참석 조사 항목이 바뀌는 말(예: 30분 늦어요 → 도착 시간, 2차는 빠질게요 → 2차 X, 술은 못 마셔요 → 음주 여부 X, 못 가요 → 참석 불참). "
        "updates 에 [{key: 항목, value: 값}] 을 항목 형식에 맞춰 넣는다. 참석 조사 항목이 없으면 update 대신 question 으로 보고 [응답하기]가 열리면 알려 달라고 답한다.\n"
        "- clarify: 본인 변경 같지만 애매함(예: 아마 갈 듯?). 시트는 바꾸지 말고 무엇으로 적을지 되묻는다.\n"
        "- other: 다른 사람에 대한 말(예: 민수 못 온대요)이나 총무가 결정할 일. 시트는 바꾸지 않고 총무에게 전하겠다고 답하고 owner_note 에 적는다. "
        "투표하겠다는 말(예: 전 오이도횟집이요)은 봇이 대신 투표할 수 없으니 위 투표 메시지에서 그 번호 이모지를 눌러 달라고 안내한다(kind 는 question).\n"
        f"- chat: 인사·잡담·농담·감탄·혼잣말처럼 모임과 직접 상관없는 말. {steer}\n"
        "- ignore: 'ㅋㅋ'·'넵'·'ㅇㅇ'·이모티콘'만' 있는 아주 짧은 반응, 또는 특정한 다른 사람에게만 하는 말(예: 지민아 어제 그거 봤어?). 대답하지 않는다(reply 빈 문자열). "
        "'ㅋㅋㅋ 다음에 같이 가요'처럼 웃음 뒤에 말이 이어지면 ignore 가 아니다.\n"
        "규칙: 정산 금액·계좌 같은 개인 금액은 단체방에서 말하지 않고 '개인 DM으로 확인해 달라'고 한다. reply 는 성격 말투로 1~2문장, 70자 안팎, "
        "말한 사람을 부를 땐 이름을 쓴다. 최근 대화에서 봇이 이미 한 말을 그대로 반복하지 않는다. update 면 무엇으로 적었는지 reply 에 짧게 말한다.")
    convo = "\n".join(f"{who}: {t}" for who, t in history[-10:]) or "(없음)"
    raw = claude(system, f"최근 단체방 대화(오래된 것부터):\n{convo}\n\n새 메시지 — {name}: {text}", max_tokens=1500, schema=CHAT_SCHEMA)
    r = json.loads(raw)
    r["updates"] = {u["key"]: u["value"] for u in r.get("updates", []) if u.get("key")}
    return r


def chat_handle(ch, uid, text, ts, thread_ts, client):
    """단체방 메시지 하나 처리. 봇이 만든 모임 채널(meet.json 에 있는 채널)에서만"""
    meet = meet_load().get(ch)
    if not meet or not text.strip() or not LLM_KEY:
        if not meet:
            print(f"단체방 메시지 무시 ({ch}: 대시보드가 모임 정보를 아직 안 보낸 채널)")
        return
    if meet.get("ended"):  # 정산까지 끝난 모임은 단체방 대화에 더 끼어들지 않음
        print(f"단체방 메시지 무시 ({ch}: 정산이 끝난 모임)")
        return
    name = meet.get("names", {}).get(uid) or "참가자"
    hist = HISTORY.setdefault(ch, [])
    now = time.time()
    if now - LAST_SEEN.get((ch, uid), 0) < 3:  # 같은 사람이 3초 안에 연달아 보낸 건 대화 기록에만 남김
        hist.append((name, text[:200]))
        return
    LAST_SEEN[(ch, uid)] = now
    sv = survey_get(ch)
    try:
        r = chat_think(meet, sv if sv.get("questions") else None, uid, name, text, hist, STREAK.get(ch, 0))
    except Exception as e:
        print("대화 판단 실패:", e)
        hist.append((name, text[:200]))
        return
    hist.append((name, text[:200]))
    kind, reply = r.get("kind", "ignore"), (r.get("reply") or "").strip()
    STREAK[ch] = STREAK.get(ch, 0) + 1 if kind == "chat" else 0  # 잡담이 몇 번 오갔는지 (2번 넘으면 본론으로)
    applied = {}
    if kind == "update" and sv.get("questions"):
        qmap = {q["key"]: q for q in sv["questions"]}
        for k, v in (r.get("updates") or {}).items():
            val = valid_value(qmap[k], v) if k in qmap else None
            if val is not None:
                applied[k] = val
        if applied:
            with LOCK:
                db = survey_load()
                u = db.setdefault(ch, {"questions": [], "users": {}}).setdefault("users", {}).setdefault(uid, {})
                u.update(applied)
                u["_at"] = time.strftime("%m/%d %H:%M:%S")
                u["_src"] = "chat"
                u["_said"] = text[:80]
                survey_save(db)
        else:
            kind = "clarify" if reply else "ignore"
    ev = {"at": time.strftime("%H:%M"), "uid": uid, "name": name, "text": text[:120], "kind": kind,
          "applied": applied, "reply": reply if kind != "ignore" else "", "owner": (r.get("owner_note") or "").strip()}
    print(f"단체방 대화 · {name}: {text[:40]!r} → {kind} {applied or ''}")
    CHAT.setdefault(ch, []).insert(0, ev)
    del CHAT[ch][30:]
    if kind != "ignore" and reply:
        # 단체방에 바로 대답 (스레드 댓글 X). 사람이 스레드 안에서 말했을 때만 그 스레드에
        client.chat_postMessage(channel=ch, text=reply, **({"thread_ts": thread_ts} if thread_ts else {}),
                                **sender(meet.get("persona"), "총무봇", ":frog:"))
        hist.append((f"총무봇({PERSONAS.get(meet.get('persona'), PERSONAS['active'])[0]})", reply))
    del hist[:-20]


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
            "/api/chat": lambda: chat_events(q.get("channel", "")),
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
            "/api/meet": lambda: meet_save(data),
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
