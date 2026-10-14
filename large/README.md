# 대모임(LARGE) 총무 Agent — 독촉부터 정산까지

담당: 채총(박채원). 300명·25개 부서 규모 워크숍의 **전체 명단 → 부서별 묶기 → 개인 인원조사 → 부서 담당자 일괄 납부 → 마감 기준표 저장 → 행사 후 정산 보고서 한 장**을 맡습니다. 소모임(SMALL, 도총)과 같은 사이트·같은 공용 서버에서 동작하며, 총무봇 **시작하기 → 모임 종류의 워크샵 카드**로 들어옵니다(모임 규모 선택 화면은 없앰).

> 시연 초점: 소통(독촉·알림)은 도총의 소모임 시연이 맡고, 대모임은 **정산 Agent** — 행사가 끝난 뒤 참석 여부·회비 차이를 총무가 하나하나 비교하지 않고 보고서 한 장(추가 납부·환불·확인 필요)과 **총무 Agent 질문**("경영지원팀의 추가 회비 납부자를 알려줘")으로 보는 것.

| 사용자 | 들어오는 방법 | 하는 일 |
|---|---|---|
| 총무 | `/large/admin/login` (서버 검증 비밀번호) | 전체 명단 CSV 업로드(부서별 묶기·담당자 자동 지정), 거래내역 업로드, 예외 승인, **정산 보고서**, 총무 Agent 질문, 담당자 안내 발송 |
| 참가자(개인) | Slack DM 의 개인 초대 링크 `/large/invite#t=…` | 참석+버스 / 참석+자차 / 불참 / 미정, Agent 채팅으로 변경 요청 |
| 팀 담당자(팀장) | `/large/manager/login` (명단 ID → Slack DM 인증코드) | 부서 회비를 한 번에 걷어 납부, 확인 질문 답변, 정산 명세 확인. 환불·추가 납부 안내는 **담당자에게만** 감 |

연락 경로는 **Slack DM**(소모임과 같은 봇·같은 토큰)입니다. 인원조사는 개인에게, 회비는 부서 담당자에게만. 은행 API·실제 송금은 범위 밖입니다.

### 명단 CSV
열: `ID, 이름, 전화번호, Slack ID, 소속 부서, 팀 담당자`. 소속 부서 하나 = 팀 하나(T01… 자동), 팀 담당자 칸에는 그 부서 팀장의 **ID 나 이름**. 같은 부서에 다른 담당자가 섞이거나·명단에 없거나·동명이인이거나·Slack ID 형식이 틀리면 **파일 전체를 반려**하고 행 번호를 보여 줍니다. 예시: [docs/samples/roster_example.csv](docs/samples/roster_example.csv), 오류 예시: [docs/samples/roster_with_errors.csv](docs/samples/roster_with_errors.csv).

### 마감 정보 저장 → 정산 보고서
| 언제 | 무엇을 저장 | 테이블 |
|---|---|---|
| 참석 마감 | 사람별 마감 기준 응답·회비(수정 안 함) | `deadline_snapshots` |
| 납부 마감 | **부서별 청구표**: 담당자가 걷어 낸 기준 금액(수정 안 함) | `team_billings` |
| 그 뒤 계속 | 마감 후·당일 변동, 입금·배정, 환불 지급(추가만) | `change_logs`, `bank_transactions`/`allocations`, `payouts` |
| 변동이 생길 때마다 | 정산 Agent 가 위를 합쳐 **보고서 새 버전**(같으면 유지) + 이전 버전과 다른 점 | `settlement_reports` |

부서 정산 잔액 = (청구액 − 확정 입금) + (최종 회비 − 청구액) + 지급 환불 → `+` 추가 납부 / `−` 환불 / 근거가 남으면 확인 필요. 자세한 계약: [docs/api-contract.md §14·15](docs/api-contract.md).

## 실행

### 최종 실행 방식 — 공용 서버 하나 (저장소 루트에서)
> 공용 서버(`hub/`)는 공용 영역이라 별도 PR(`feat/integration`)로 올립니다. 그 PR 이 머지되기 전에는 아래 'LARGE 단독'으로 실행합니다.

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r hub/requirements.txt   # 처음 한 번
(cd large/frontend && npm ci && npm run build)                              # LARGE 화면 빌드 → large/frontend/dist
(cd large/backend && ../../.venv/bin/python -m app.seed --reset)             # 가상 300명·25부서, 행사 어제 끝난 상태 (선택)
.venv/bin/python -m hub                                                      # http://127.0.0.1:8787
```
`http://127.0.0.1:8787` → 시작하기 → **워크샵** → `/large/` (공용 서버가 꺼져 있으면 대모임 안내 카드). 화면 왼쪽 위 **← 총무봇 시작 화면**으로 돌아갑니다.

- 시연 총무 계정: `admin@example.com` / `demo1234!` (시연 모드 기본값, `ADMIN_EMAIL`·`ADMIN_PASSWORD` 로 변경)
- 시연용 역할 전환·개발용 수신함(모의 Slack DM): `/large/` 첫 화면 (시연 모드에서만)
- 시드 단계: 기본 `--stage after-event`(행사 다음 날 — 정산 보고서 시연), `--stage before-event`(행사 5일 전 — 참석 변경·납부 시연)
- 이전(이메일) 버전 DB 는 구조가 달라 서버가 시작하지 않고 `app.seed --reset` 안내를 띄웁니다.

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

## 발표 시연
초기화·실행 명령, 5~7분 장면 순서, 장면별 사람/LLM/코드 역할, 실측 기록: **[docs/demo-script.md](docs/demo-script.md)**
```bash
(cd large/backend && ../../.venv/bin/python -m app.seed --reset)   # 시연용 DB 만 초기화(표시 없는 DB·운영 모드는 거부)
.venv/bin/python -m hub
```

## GitHub Pages 정적 데모판
데모 링크(https://greatdh84.github.io/chongmubot/demo/)는 서버가 없는 정적 사이트입니다. 거기서 **시작하기 → 워크샵**을 누르면
`demo/large/`의 정적 데모판(`#/admin`)이 열립니다. 공용 서버(`python -m hub`)로 연 경우에는 지금처럼 실제 화면 `/large/`로 갑니다
(`demo/index.html` 대모임 블록이 `/api/large/meta` 응답 여부로 판단).

| 되는 것 | 안 되는 것 |
|---|---|
| 총무 화면 5개 탭(대시보드·부서·명단·입금·확인·정산 보고서·기록), 부서 클릭·명단 검색(브라우저에서 거름)·정산 명세, 보고서 CSV 내려받기 | 저장·발송 전부(명단·거래내역 업로드, 승인, 배정 확정, 다시 만들기 등) → "정적 데모라 저장되지 않아요" |
| 총무 Agent: 예시 버튼 6개 + 녹화한 질문(인사팀·재무팀 현황 등), "환불 대상 담당자에게 안내 보내줘 → 네/아니요" | 녹화하지 않은 질문(안내 문구가 나옴), 참가자·담당자 화면, 로그인 |

- 데이터는 시드(가상 300명·25개 부서, 행사 다음 날 상태, 기준일 2026-10-15)이고, 화면이 부르는 GET 응답과 Agent 답을 그대로 녹화한 것이라 **금액은 서버 코드 계산값과 같습니다**. 이름·전화(010-0000-xxxx)·Slack ID(UDEMO…)는 모두 가상, 실제 키는 쓰지 않습니다(`NOTIFY_MODE=mock`, `LLM_MODE=mock`).
- 구성: `npm run build:static`(= `vite --mode static`, 상대 경로·HashRouter, `src/staticDemo.ts`가 API 대신 응답) → `demo/large/index.html`, `assets/` · `backend/scripts/export_static_demo.py` → `demo/large/fixtures.json`, `report.csv`.

**다시 만들기**: 정적 데모판은 스냅샷이라 `large/`를 고치면 다시 만들어야 합니다.
```bash
large/scripts/update_static_demo.sh    # 저장소 안 어디서든. npm ci · build:static → 응답 녹화, 실패하면 멈춤
git status demo/large                  # 바뀐 파일 확인 후 커밋
```
**자동 갱신**: `.github/workflows/large-static-demo.yml` 은 main 에 `large/**` 변경이 push 되면 위 스크립트를 돌리고, `demo/large/`가 바뀌었을 때만
`chore: 대모임 정적 데모판 갱신 [skip ci]`로 main 에 커밋합니다(github-actions 봇). 녹화 시각을 기준일로 고정해서 코드가 같으면 파일도 같으므로 빈 커밋은 생기지 않습니다. Pages 배포 설정은 바꾸지 않습니다.

## 실제 Slack DM · 실제 LLM 켜기
- Slack: 저장소 루트 `.env` 에 소모임이 쓰는 `SLACK_BOT_TOKEN` 이 있으면, `large/backend/.env` 에 `NOTIFY_MODE=slack` → 서버 재시작 → 상단 태그 `Slack DM 실제`. ⚠️ 수업 워크스페이스에 실제 DM 이 갑니다. 시연용 명단의 Slack ID 는 본인·채총처럼 소수만 실제 ID 로 바꾸세요(시드 ID `UDEMO…` 는 존재하지 않아 실패로 기록됩니다). 실제 Slack 모드에서는 스케줄러가 기본 꺼짐(`NOTIFY_SCHEDULER=on` 으로 켬).
- Claude: 루트 `.env` 의 `LLM_API_KEY`(소모임과 같은 키) + `large/backend/.env` 에 `LLM_MODE=claude` (기본 모델 `claude-haiku-5-5`, `CLAUDE_MODEL` 로 변경)
- Gemini: `LLM_MODE=gemini`, `GEMINI_API_KEY=…` (점검: `scripts/llm_live_check.py`)

모델이 실패하거나 형식이 틀린 출력을 내면 그 턴은 규칙 기반으로 대체되고, 말풍선과 Agent 기록에 "LLM 실패 → 규칙 기반 대체"와 사유가 남습니다. 잘못된 출력은 실행하지 않습니다.

## 알림(Slack DM) 발송 스케줄러
- 서버 안 백그라운드 스레드가 `EMAIL_SCHEDULER_INTERVAL`(기본 60초)마다 발송 큐를 처리하고, 마감이 지났으면 **기준표를 저장하고 보고서를 갱신**합니다. `NOTIFY_SCHEDULER=on|off` — 기본은 모의 발송이면 켜짐, 실제 Slack 이면 꺼짐(실수 발송 방지). 총무 화면 기록 탭에서 일시정지·재개.
- 발송 직전 재검사(응답·입금하면 독촉 취소), 21~8시 금지, 수신자별 하루 2건, 3회 재시도, 중복 키는 그대로.
- 수동 "지금 발송 실행"과 겹쳐도 같은 메일은 한 번만: 같은 프로세스는 잠금, 다른 프로세스는 DB 선점(`queued→sending`).
- 지원 범위: **서버 1개 프로세스 권장.** 여러 프로세스(uvicorn workers 등)여도 중복 발송은 선점으로 막지만, 발송 도중 프로세스가 죽은 1건은 실제로 나갔는지 알 수 없어 10분 뒤 `failed`+"확인 필요" 메모로 남기고 자동 재발송하지 않습니다. 각 프로세스가 스케줄러를 띄우므로 간격이 그만큼 촘촘해집니다.
- 서버 종료 시 스레드를 멈춥니다. 실제 Slack DM 발송은 이번 작업에서 실행하지 않았습니다(모의 발송·테스트로만 확인).

## 구성
```
large/backend/app/
  models.py      행사(mode='large')·참가자·팀·담당자·초대·세션·응답·마감 기준 응답·변경 이력·원본 입금·입금 배정·확인 과제·채팅·이메일 작업·정산·송금
  services/      금액 계산(fees)·응답/변경·입금 배정·정산·초대·이메일 — 금액·권한·중복 방지는 모두 여기 코드
  agents/        소통 Agent(LLM 이 다음 행동 선택) · 정산 처리(규칙 R1~R6, LLM 미사용) · 총무 Agent(보고서 질문·담당자 안내) · 도구 계약
  services/report.py  마감 기준표(청구표) 저장 · 정산 보고서 계산·버전 저장·CSV
  adapters/      email(=Slack DM: mock/slack) · llm(mock/claude/gemini)
  routers/       /api/large/{auth,me,team,admin,dev}
  main.py        configure_large(app) · add_large_ui(app) — 공용 서버가 가져다 씀
large/frontend/  React + TS (base /large/)
large/docs/      PRD · API 계약 · 워크플로(적용 전/후·HITL) · 발표 시연(demo-script) · 디자인(design)
```

## AI 가 하는 일과 코드가 하는 일
| | 담당 |
|---|---|
| 요청 해석, 관련 기록 조회, 부족한 정보 판단, 다음 도구 선택 | 소통 Agent·총무 Agent 의 LLM (`LLM_MODE=mock` 이면 규칙 기반 모의, `claude`/`gemini` 면 실제 LLM) |
| 마감 기준표 저장, 보고서 계산·버전·차이 | **정산 Agent(규칙 기반 코드)** |
| 금액 계산, 환불 규칙 적용, 접근 통제, 중복 방지, '네/아니요' 판정, 갱신 실행 | 코드 |
| 입금 대조·확인 과제 생성·답변 재검토 | **규칙 기반 코드**(LLM 없음). 질문 문구는 템플릿 |

소모임과 같은 Claude 키로 통일할 수 있게 `claude` 모드를 추가했습니다(Gemini·모의도 유지). 키가 없으면 화면 위 띠에 "LLM 모의(규칙)"로 표시됩니다.

## 모드
| 변수(`large/backend/.env`) | 값 | 의미 |
|---|---|---|
| `APP_MODE` | `demo`(기본) / `production` | production 은 `/api/large/dev/*`(역할 전환·수신함) 미등록, Secure 쿠키, `SECRET_KEY`·관리자 계정 필수 |
| `NOTIFY_MODE` | `mock`(기본) / `slack` | mock 은 실제로 보내지 않고 개발용 수신함에만 기록. slack 은 루트 `.env` 의 `SLACK_BOT_TOKEN` |
| `LLM_MODE` | `mock`(기본) / `claude` / `gemini` | claude 는 루트 `.env` 의 `LLM_API_KEY`, gemini 는 `GEMINI_API_KEY` |
| `PUBLIC_BASE_URL` | 기본 `http://127.0.0.1:8787/large` | 메일 속 링크 주소 |
| `NOTIFY_SCHEDULER` / `EMAIL_SCHEDULER_INTERVAL` | 모의 발송이면 `on` / `60` | 발송 큐 자동 실행 + 마감 기준표 저장 |

변수 이름은 `large/backend/.env.example`. 소모임과 같이 쓰는 키(`SLACK_BOT_TOKEN`, `LLM_API_KEY`)만 저장소 루트 `.env` 에서 읽고, 나머지는 `large/backend/.env` 입니다.

## 이번 단계에서 막아 둔 위험
- 열린 확인 과제가 여럿일 때 번호 없는 "네"는 아무 과제에도 답하지 않고 되묻습니다. 확인 질문 뒤에 새 알림이 끼면 "네"를 실행하지 않고 다시 확인합니다.
- 환불 규칙이 없는 마감 후 변경은 환불액 **미정**(임시 0원)으로 표시되고, 총무가 규칙을 추가하거나 환불액·사유를 직접 정하기 전엔 승인·정산 확정이 되지 않습니다.
- LARGE API 는 `mode='large'` 행사만 다루며 모드는 바꿀 수 없습니다.

## 미구현
실제 Slack DM 발송 검증(수업 워크스페이스), Slack 답장 수집, 행사 생성 화면, 통합 로그인, i18n·접근성 점검, 배포. 공용 시작 화면에서 총무 로그인을 한 번에 하는 통합 인증은 하지 않았습니다(`docs/integration-plan.md` 참고).
