# 대모임(LARGE) 총무 Agent — 독촉부터 정산까지

담당: 채총(박채원). 300명·25개 팀 규모 워크숍의 **개인 참석 응답 → 팀 청구 → 입금 대조 → 변경·불일치 조사 → 확인 대화 → 정산안 갱신**을 맡습니다. 소모임(SMALL, 도총)과 같은 사이트·같은 공용 서버에서 동작하며, 총무봇 시작 화면의 **모임 규모 선택 → 대모임**으로 들어옵니다.

| 사용자 | 들어오는 방법 | 하는 일 |
|---|---|---|
| 총무 | `/large/admin/login` (서버 검증 비밀번호) | 행사·회비·환불 규칙, 명단·거래내역 업로드, 담당자 지정, 예외 검토, 정산 확정, 송금 결과 기록 |
| 참가자 | 이메일 개인 초대 링크 `/large/invite#t=…` | 참석+버스 / 참석+자차 / 불참 / 미정, Agent 채팅으로 변경 요청 |
| 팀 담당자 | `/large/manager/login` (이메일 인증코드) | 자기 팀 현황·청구·확인 질문 답변·정산 명세 |

연락 경로는 **이메일 + 모바일 웹**입니다. Slack 은 쓰지 않습니다(소모임만 Slack). 이메일 답장 수집·은행 API·실제 송금은 범위 밖입니다.

## 실행

### 최종 실행 방식 — 공용 서버 하나 (저장소 루트에서)
> 공용 서버(`hub/`)는 공용 영역이라 별도 PR(`feat/integration`)로 올립니다. 그 PR 이 머지되기 전에는 아래 'LARGE 단독'으로 실행합니다.

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r hub/requirements.txt   # 처음 한 번
(cd large/frontend && npm ci && npm run build)                              # LARGE 화면 빌드 → large/frontend/dist
(cd large/backend && ../../.venv/bin/python -m app.seed --reset)             # 가상 300명·25팀 (선택)
.venv/bin/python -m hub                                                      # http://127.0.0.1:8787
```
`http://127.0.0.1:8787` → 시작하기 → 모임 규모 **대모임** → "대모임 관리 화면 열기" → `/large/`. 화면 왼쪽 위 **← 총무봇 시작 화면**으로 돌아갑니다.

- 시연 총무 계정: `admin@example.com` / `demo1234!` (시연 모드 기본값, `ADMIN_EMAIL`·`ADMIN_PASSWORD` 로 변경)
- 시연용 역할 전환·개발용 수신함: `/large/` 첫 화면 (시연 모드에서만)

### 개발 중 — Vite 개발 서버 (최종 실행 방식 아님)
```bash
.venv/bin/python -m hub                     # API 는 공용 서버가 제공
cd large/frontend && npm run dev            # http://localhost:5173/large/  (/api/large 를 8787 로 프록시)
```
화면 위 띠에 "Vite 개발 서버(최종 실행 방식 아님)"가 표시됩니다. 초대 메일 링크는 `PUBLIC_BASE_URL`(기본 `http://127.0.0.1:8787/large`) 기준이고, 개발용 수신함에서 누르면 지금 열린 주소에서 열립니다.

### LARGE 단독 (테스트용)
```bash
cd large/backend && ../../.venv/bin/uvicorn app.main:app --port 8000   # /large/, /api/large/*
```

### 검사
```bash
cd large/backend && ../../.venv/bin/python -m pytest -q     # LARGE 테스트
cd large/frontend && npm run typecheck && npm run build
.venv/bin/python -m pytest -q hub/tests                     # 공용 서버(루트에서)
```

## 구성
```
large/backend/app/
  models.py      행사(mode='large')·참가자·팀·담당자·초대·세션·응답·마감 기준 응답·변경 이력·원본 입금·입금 배정·확인 과제·채팅·이메일 작업·정산·송금
  services/      금액 계산(fees)·응답/변경·입금 배정·정산·초대·이메일 — 금액·권한·중복 방지는 모두 여기 코드
  agents/        소통 Agent(LLM 이 다음 행동 선택) · 정산 처리(규칙 R1~R6, LLM 미사용) · 도구 계약
  adapters/      email(mock/smtp) · llm(mock/gemini)
  routers/       /api/large/{auth,me,team,admin,dev}
  main.py        configure_large(app) · add_large_ui(app) — 공용 서버가 가져다 씀
large/frontend/  React + TS (base /large/)
large/docs/      PRD · API 계약 · 워크플로(적용 전/후·HITL)
```

## AI 가 하는 일과 코드가 하는 일
| | 담당 |
|---|---|
| 요청 해석, 관련 기록 조회, 부족한 정보 판단, 다음 도구 선택 | 소통 Agent 의 LLM (`LLM_MODE=mock` 이면 규칙 기반 모의, `gemini` 면 Gemini) |
| 금액 계산, 환불 규칙 적용, 접근 통제, 중복 방지, '네/아니요' 판정, 갱신 실행 | 코드 |
| 입금 대조·확인 과제 생성·답변 재검토 | **규칙 기반 코드**(LLM 없음). 질문 문구는 템플릿 |

LLM 제공업체는 아직 확정하지 않았습니다. 기존 Gemini·모의 어댑터를 그대로 두었고, 키가 없으면 화면 위 띠에 "LLM: 모의(규칙 기반, API 키 없이 동작)"로 표시됩니다.

## 모드
| 변수(`large/backend/.env`) | 값 | 의미 |
|---|---|---|
| `APP_MODE` | `demo`(기본) / `production` | production 은 `/api/large/dev/*`(역할 전환·수신함) 미등록, Secure 쿠키, `SECRET_KEY`·관리자 계정 필수 |
| `EMAIL_MODE` | `mock`(기본) / `smtp` | mock 은 실제로 보내지 않고 개발용 수신함에만 기록 |
| `LLM_MODE` | `mock`(기본) / `gemini` | gemini 는 `GEMINI_API_KEY` 필요(실제 키로는 미검증) |
| `PUBLIC_BASE_URL` | 기본 `http://127.0.0.1:8787/large` | 메일 속 링크 주소 |

변수 이름은 `large/backend/.env.example`. 소모임의 `.env`(저장소 루트)와는 파일이 다르고 변수 이름도 겹치지 않습니다.

## 이번 단계에서 막아 둔 위험
- 열린 확인 과제가 여럿일 때 번호 없는 "네"는 아무 과제에도 답하지 않고 되묻습니다. 확인 질문 뒤에 새 알림이 끼면 "네"를 실행하지 않고 다시 확인합니다.
- 환불 규칙이 없는 마감 후 변경은 환불액 **미정**(임시 0원)으로 표시되고, 총무가 규칙을 추가하거나 환불액·사유를 직접 정하기 전엔 승인·정산 확정이 되지 않습니다.
- LARGE API 는 `mode='large'` 행사만 다루며 모드는 바꿀 수 없습니다.

## 미구현
실제 SMTP 발송 검증, 발송 큐 자동 스케줄러(지금은 총무가 '지금 발송 실행'), 행사 생성 화면, Gemini 실제 키 검증, i18n·접근성 점검, 배포. 공용 시작 화면에서 총무 로그인을 한 번에 하는 통합 인증은 하지 않았습니다(`docs/integration-plan.md` 참고).
