# 총무 Agent: 독촉부터 정산까지

300명·25개 팀 워크숍의 **참석 변경**과 **팀별 입금**을 대조하고, 필요한 확인 대화를 거쳐 **근거 있는 회비 정산안**을 만드는 업무용 AI Agent 앱입니다. (Hands-on AI Bootcamp, Category C · 2인 팀)

- 총무: 행사·회비·환불 규칙 설정, 명단 업로드, 담당자 지정, 거래내역 업로드, 확인 과제 검토, 정산 확정, 송금 결과 기록
- 참가자: 이메일의 개인 링크 → 참석+버스 / 참석+자차 / 불참 / 미정 선택, Agent 채팅으로 변경 요청
- 팀 담당자: 이메일 인증 → 자기 팀 응답·청구·변경·정산 명세 확인, Agent 확인 질문에 답변

연락 경로는 **이메일(알림) + 모바일 웹(응답·대화)** 뿐입니다. Slack·이메일 답장 수집·은행 API·실제 송금은 범위 밖입니다.

## 구성

| 영역 | 기술 |
|---|---|
| 백엔드 | Python 3.11 · FastAPI · SQLAlchemy 2 · SQLite(WAL) |
| 프런트 | React 19 · TypeScript · Vite · react-router |
| 이메일 | 어댑터: `mock`(개발용 수신함) / `smtp` |
| LLM | 어댑터: `mock`(규칙 기반) / `gemini`(REST, 모델 폴백) |

```
backend/app/
  models.py            데이터 모델(행사·참가자·팀·담당자·초대·세션·응답·마감 기준 응답·변경 이력·원본 입금·입금 배정·확인 과제·채팅·이메일 작업·정산·송금)
  services/            금액 계산(fees)·응답/변경·입금 배정·정산·초대·이메일 발송(코드로만 수행)
  agents/              소통 Agent(communication)·정산 Agent(settlement_agent)·도구 계약(tools)
  adapters/            email.py · llm.py
  routers/             auth · participant(/api/me) · manager(/api/team) · admin(/api/admin) · dev(시연 전용)
  seed.py              가상 300명·25팀 시연 데이터
backend/tests/         금액·권한·중복 배정·Agent·이메일·시드 테스트
frontend/src/          총무·참가자·담당자 화면
docs/                  PRD · API 계약 · 워크플로(적용 전/후) · 과제 안내
```

## 설치·실행 (외부 키 없이 로컬)

```bash
# 1) 백엔드
cd backend
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt   # 또는 uv venv && uv pip install -r requirements.txt
.venv/bin/python -m app.seed --reset            # 가상 데이터 생성(오늘 기준 날짜). 날짜까지 고정: --base-date 2026-10-08
.venv/bin/uvicorn app.main:app --port 8000

# 2) 프런트 (다른 터미널)
cd frontend
npm install
npm run dev                                     # http://localhost:5173  (/api 는 8000 으로 프록시)
```

- 시연 총무 계정: `admin@example.com` / `demo1234!` (시연 모드 기본값. `ADMIN_EMAIL`·`ADMIN_PASSWORD` 로 변경)
- 첫 화면의 **시연용 역할 전환**으로 총무·담당자·참가자 화면을 바로 볼 수 있습니다.
- 실제 흐름 확인: 화면 상단 **개발용 수신함** → 초대 메일의 링크 클릭(참가자) / 담당자 로그인 후 받은 인증코드 확인(담당자).
- 단일 서버로 보기: `cd frontend && npm run build` 후 백엔드만 실행하면 `http://localhost:8000` 에서 화면까지 제공합니다.

### 초기화
```bash
cd backend && .venv/bin/python -m app.seed --reset      # backend/var/chongmu.db 삭제 후 재생성
```
시드는 `backend/var/samples/` 에 `roster_synthetic.csv`, `transactions_batch1.csv`(시드에 이미 반영), `transactions_batch2.csv`(시연 중 업로드용: T24 입금 + 1차와 같은 행 1개)를 만듭니다. 모두 가상 데이터이며 Git 에서 제외됩니다.

### 검사
```bash
cd backend && .venv/bin/python -m pytest -q        # 63 tests
cd frontend && npm run typecheck && npm run build
```

## 모드 — 모의 vs 실제 연동

화면 맨 위 띠에 현재 모드가 항상 표시됩니다(노란 띠 = 시연, 초록 띠 = 운영).

| 변수 | 값 | 의미 |
|---|---|---|
| `APP_MODE` | `demo`(기본) | 시연용 역할 전환·개발용 수신함·API 문서(`/api/docs`) 사용 가능, 시드 생성 가능 |
| | `production` | `/api/dev/*` 라우터 **미등록**(역할 전환 불가), 쿠키 `Secure`, `SECRET_KEY`(32자+)·`ADMIN_EMAIL`·`ADMIN_PASSWORD` 없으면 기동 거부 |
| `EMAIL_MODE` | `mock`(기본) | 발송하지 않고 `dev_inbox` 테이블(개발용 수신함)에 기록. 주소에 `bounce` 가 있으면 실패를 흉내 냄 |
| | `smtp` | `SMTP_HOST/PORT/USER/PASSWORD/FROM` 으로 실제 발송 (이번 단계에서 실제 발송 검증은 하지 않음) |
| `LLM_MODE` | `mock`(기본) | 규칙 기반 의도 해석. 결정적이라 테스트·시연에 사용 |
| | `gemini` | `GEMINI_API_KEY` 필요. `gemini-flash-latest` → `gemini-flash-lite-latest` 순서로 404·429·503 시 다음 모델. 실패하면 규칙 기반으로 대체하고 실행 기록에 남김 |

모든 비밀값은 `backend/.env`(Git 제외) 또는 서버 환경변수로만 넣습니다. 변수 이름 목록은 `backend/.env.example` 참고.

## 보안·접근 요약
- 총무: 서버 검증 비밀번호 로그인(scrypt 해시, 15분 5회 실패 제한). 자기 행사만 조회(남의 행사·하위 리소스는 404).
- 참가자: 무작위 초대 토큰(32바이트) → **HMAC 해시만 저장**, 30일 만료·재발급(기존 폐기)·폐기 지원. 링크는 `/invite#t=…` 로 토큰을 URL fragment 에 두어 서버 로그에 남지 않고, 화면이 즉시 주소창에서 지운 뒤 `POST /api/auth/invite/exchange` 로 세션 쿠키(HttpOnly, SameSite=Lax)로 바꿉니다.
- 담당자: 총무가 지정한 담당자 이메일로 6자리 코드(10분, 5회 시도, 시간당 5회) → 담당자 세션. 요청마다 "현재도 담당자인지" 재확인.
- GET 은 상태를 바꾸지 않습니다. 모든 변경은 `X-Requested-With: chongmu` 헤더가 있는 POST/PUT/DELETE 로만.
- 로그 필터가 토큰 형태 문자열을 가립니다. 메일에는 금액 명세 대신 웹 확인 링크만 넣습니다.

## 미구현·다음 단계
- 실제 SMTP 발송 검증, 발송 큐를 주기적으로 돌리는 스케줄러(지금은 총무가 '지금 발송 실행' 버튼으로 실행)
- 이메일 답장 자동 수집, Slack, 은행 API·실제 송금(범위 밖)
- 행사 생성 화면(지금은 시드로 1개 행사 생성), 여러 행사 전환 UI, 총무 계정 여러 명
- Gemini 모드의 실제 키 대화 품질 점검, 정산 Agent 질문 문구의 LLM 생성(지금은 템플릿)
- i18n, 접근성 점검(Lighthouse/WCAG), 배포
- 담당자 1명/팀 제약(부담당자 없음)
