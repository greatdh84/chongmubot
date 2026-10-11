# 공용 서버 통합안 (SMALL + LARGE 한 사이트·한 서버)

작성: 채총(박채원) · 2026-10-12 · 대상 브랜치 `feat/integration` (기반: `feat/large`)
이 문서와 `hub/`, 루트 README 의 실행 안내는 **공용 영역**이라 도총 확인 후 머지합니다. 소모임 코드(`app/server.py`, demo 의 소모임 블록·공용 부분)는 한 줄도 바꾸지 않았습니다.

## 1. 확인한 현재 상태 (2026-10-12, `origin/main` = `ff916c3`)

| 항목 | 실제 상태 | 근거 |
|---|---|---|
| SMALL 화면 | `demo/index.html` 한 파일. 상태는 브라우저 localStorage `chongmubot-demo-v3` | 파일 확인 |
| SMALL 서버 | `app/server.py` — 표준 라이브러리 `ThreadingHTTPServer`, 127.0.0.1:8787, `/` → `/demo/index.html`, `demo/` 정적 제공 | 코드 확인 |
| SMALL API | `/api/config` `/api/socket` `/api/survey` `/api/slack/{members,channel,invite,send,poll,thread,cancel}` `/api/survey/start` `/api/pay/parse` `/api/receipt/parse` `/api/places/{recommend,search,nearby}` | `do_GET`/`do_POST` 라우트 표 |
| SMALL 화면 → API 주소 | `SERVED = location.pathname.startsWith('/demo/')` 이면 같은 출처, 아니면 `127.0.0.1:8787` | `demo/index.html` 356~358행 |
| 카카오 지도 | JS 키 도메인이 `127.0.0.1:8787`·`localhost:8787` 로 등록 → 공용 서버도 8787 이어야 지도 유지 | 작업노트 |
| import 부작용 | 루트 `.env` 를 환경변수로 읽기(setdefault), `SSL_CERT_FILE` 기본값, 상수 읽기뿐. Socket Mode·서버 시작은 `__main__` 안 | 코드 확인 + `hub/tests` 의 부작용 테스트 |
| LARGE | `large/` (FastAPI + SQLite + React). 기존 대모임 블록은 "준비 중" placeholder 였음 | `feat/large` |
| 문서와 다른 점 | 작업노트에는 "feat/slack 8커밋 미병합"이라 적혀 있으나 실제로는 PR #5 로 main 에 병합됨. `feat/large` 는 main 보다 뒤(7e75cdf)였고 고유 커밋 없음 → main 으로 fast-forward 후 작업 | `git log` |

## 2. 선택한 구조

**FastAPI 를 공용 진입점(`hub/`)으로 두고, SMALL 서버는 고치지 않은 채 '요청 브리지'로 연결.**

```
브라우저 ── 127.0.0.1:8787 ── hub (FastAPI, uvicorn 하나)
                               ├─ /api/large/*  → LARGE 라우터 (large/backend: configure_large)
                               ├─ /large/*      → LARGE React 빌드 (large/frontend/dist, 내부 경로는 index.html)
                               ├─ /small, /small/* → /demo/index.html 로 이동
                               ├─ /api/hub      → 두 시스템 상태
                               └─ 그 밖 전부     → SMALL 브리지 → app/server.py 의 Handler.do_GET/do_POST
                                                   (/, /demo/*, /api/slack/* 등 기존 경로 그대로)
```

브리지(`hub/small_bridge.py`)는 `app/server.py` 를 파일 경로로 import 하고, 그 안의 `Handler` 를 소켓 없이 만들어 같은 `do_GET`/`do_POST` 를 호출한 뒤 써 낸 HTTP 응답(상태·헤더·본문)을 그대로 돌려줍니다.

### 고른 이유
| 후보 | 판단 |
|---|---|
| **A. FastAPI hub + SMALL 브리지 (선택)** | SMALL 코드 변경 0줄. 경로·응답 형식·CORS·정적 제공이 원래 서버와 바이트 단위로 같음을 테스트로 비교(`test_small_behaves_like_original_server`, 14개 요청). 서버·포트·명령 하나 |
| B. SMALL 라우트를 FastAPI 로 다시 작성 | 도총 영역 대규모 변경, 발표 직전 회귀 위험 |
| C. 두 서버를 따로 띄우고 링크로 연결 | 요구사항에서 최종 구조로 쓰지 않기로 함 |
| D. SMALL 서버가 LARGE 를 프록시 | 표준 라이브러리 서버에 ASGI 를 얹어야 하고 SMALL 코드 수정 필요 |

## 3. 화면 흐름

1. `/` → SMALL 서버가 하던 대로 `/demo/index.html` (기존 **시작하기 → 모임 규모 선택** 화면을 공용 시작 화면으로 그대로 사용 — README 상 도총 담당 공용 영역)
2. 소모임 선택 → 같은 페이지의 소모임 설정(기존 그대로)
3. 대모임 선택 → 대모임 블록(채총 영역)의 진입 카드 → `/api/large/meta` 로 공용 서버를 확인하면 **대모임 관리 화면 열기** 활성 → `/large/`
   - 서버 없이 연 정적 데모(GitHub Pages, 파일)는 "정적 데모 — 대모임 화면은 공용 서버에서만" 표시, 버튼 비활성
4. LARGE 화면 왼쪽 위 **← 총무봇 시작 화면** → `/` → 시작 화면
   - 대모임 블록의 '열기' 동작이 떠나기 전에 `S.screen='start'`, (진행 중 소모임이 없으면) `S.size=null` 로 둡니다. 기존 `goStart`·`changeSize` 버튼과 같은 전환이지만 공용 상태 키를 대모임 블록에서 바꾸는 것이므로 **도총 확인 필요**(합의 사항 ①).

## 4. 경계

| 항목 | SMALL | LARGE | 공용 |
|---|---|---|---|
| 화면 | `/demo/index.html` (`/small/` 은 이리로 이동) | `/large/*` | `/` = SMALL 의 시작·규모 선택 |
| API | `/api/*` 기존 경로 그대로 | `/api/large/*` | `/api/hub` |
| 인증·세션 | 없음(로컬 총무 1명, Slack 토큰은 서버만 보유) | 총무 비밀번호 / 참가자 초대 토큰 / 담당자 이메일 코드, 쿠키 Path=`/api/large` | 통합 로그인 **없음** |
| 행사 식별 | 브라우저 localStorage + Slack 채널 ID | DB `events.id`, `mode='large'` 만 허용, 모드 변경 금지 | — |
| 데이터 | `app/data/survey.json`, localStorage | `large/backend/var/chongmu.db` | 공유 테이블 없음 |
| 비밀값 | 루트 `.env` (SLACK_*, LLM_API_KEY, KAKAO_*) | `large/backend/.env` (APP_MODE, SMTP_*, GEMINI_*, …) | 이름 충돌 없음 |
| 외부 발송 | Slack(실제 토큰 있을 때) | 이메일(mock 기본) | — |
| CSRF 헤더 규칙 | 적용 안 함(기존 동작 유지) | `/api/large/*` 상태 변경에만 | — |

- LARGE 의 `/api/large/*` 에 없는 경로는 LARGE 가 404 로 끝내고 SMALL 로 넘기지 않습니다.
- LARGE 화면·API 를 쓰는 동안 SMALL 의 `slack()`/`send()` 는 호출되지 않습니다(`test_large_traffic_never_reaches_small_slack`, 브라우저 네트워크 기록으로도 확인).
- Socket Mode: `app/server.py` 단독 실행과 같게, 공용 서버 **시작 시** `SLACK_APP_TOKEN` 이 있으면 연결합니다(import 로는 연결 안 함). `HUB_SMALL_SOCKET=off` 로 끌 수 있습니다.
- **인증은 통합하지 않았습니다.** SMALL 은 원래 로그인 없이 로컬에서 쓰는 구조이고, LARGE 는 자체 인증을 그대로 씁니다. 시작 화면·SMALL 화면에는 로그인이 없으므로 공용 서버를 외부에 공개하면 SMALL API 도 공개됩니다(현재와 동일한 전제: 127.0.0.1 바인딩).

## 5. 바뀌는 파일

| 파일 | 영역 | 브랜치 |
|---|---|---|
| `large/**` | 채총 | `feat/large` |
| `demo/index.html` 대모임 블록·대모임 `<style>` | 채총 | `feat/large` |
| `hub/__init__.py` `hub/__main__.py` `hub/main.py` `hub/small_bridge.py` `hub/requirements.txt` `hub/tests/*` | 공용(신규) | `feat/integration` |
| `docs/integration-plan.md` | 공용(신규) | `feat/integration` |
| `README.md` "공용 서버로 실행" 절 추가 | 공용 | `feat/integration` |
| `app/server.py`, demo 의 소모임·공용 부분 | 도총 | **변경 없음** |

## 6. 실행
```bash
python3.11 -m venv .venv && .venv/bin/pip install -r hub/requirements.txt
(cd large/frontend && npm ci && npm run build)
.venv/bin/python -m hub          # http://127.0.0.1:8787
```
기존 `app/.venv/bin/python app/server.py` 도 그대로 동작합니다(SMALL 단독, LARGE 없음).

## 7. 합의할 사항
1. 대모임 블록의 '열기'가 떠나기 전 `S.screen`/`S.size` 를 시작 화면 상태로 돌리는 것(§3-4) — 괜찮은지, 아니면 공용 부분에 `#start` 같은 복귀 신호를 도총이 넣을지
2. `/small/` 을 지금처럼 `/demo/index.html` 로 보낼지, demo 의 `SERVED` 판정에 `/small/` 을 추가해 그 주소에서 직접 열지(도총 영역 한 줄)
3. 공용 서버를 정식 실행법으로 README 맨 위에 올릴지(지금은 기존 `app/server.py` 안내를 두고 절만 추가)
4. GitHub Pages 정적 데모에서 대모임은 '공용 서버에서만' 안내로 충분한지
5. 통합 로그인(총무 한 번 로그인) 필요 여부 — 필요하면 SMALL 에 인증을 새로 도입해야 함
6. LLM 제공업체: SMALL 은 Claude(`LLM_API_KEY`), LARGE 는 Gemini/모의 어댑터. 하나로 맞출지
7. 발표 시연 순서(시작 → 소모임 → 시작 → 대모임)와 시연용 데이터 초기화 방법

## 8. 검증 (이번 실행)
- `hub/tests` 20개: 라우팅, 내부 경로 새로고침, 없는 자산 404, 빌드 없을 때 503 안내, SMALL 응답 동등성 14건, import 부작용 없음(스레드·네트워크), LARGE 요청이 SMALL Slack 함수를 부르지 않음, 쿠키 Path, CSRF 규칙 범위
- 브라우저(Playwright, 공용 서버): 시작 → 규모 선택 → 대모임 → `/large/` → 총무 로그인 → `/large/admin` 새로고침·직접 접근 → 시작 화면 복귀 → 소모임 진입, 초대 링크 → `/large/me`(주소창 토큰 제거)·새로고침, 파일로 연 정적 데모의 안내 표시. Slack 발송 계열 POST 0건
