# API·데이터·오류·채팅 상태 계약

실행 중이면 시연 모드에서 `http://127.0.0.1:8787/api/large/docs` 에 OpenAPI 문서가 있습니다. 이 문서는 팀원이 기준으로 삼을 **약속**을 적습니다. 바꿀 때는 이 문서와 테스트를 같이 고칩니다.

## 1. 공통
- 기본 경로 `/api/large` (총무봇 공용 서버에서 SMALL API 와 섞이지 않게 분리). 화면은 `/large/`. JSON. 시각은 KST 오프셋이 붙은 ISO 문자열로 응답(`2026-10-08T10:00:00+09:00`). 입력에 오프셋이 없으면 KST 로 해석. DB 는 naive UTC.
- 금액은 원 단위 정수. `null` = 미확정.
- 인증: 역할별 HttpOnly 쿠키 `cm_admin` / `cm_participant` / `cm_manager` (Path=/api/large — SMALL API 로는 전송되지 않음, SameSite=Lax, 운영 모드 Secure).
- 이 DB 의 행사는 `mode='large'` 만 다룹니다. 다른 모드의 행사 ID·초대·세션은 404/401. 모드는 만든 뒤 바꿀 수 없습니다(`MODE_LOCKED`).
- 상태를 바꾸는 요청(POST/PUT/PATCH/DELETE)은 헤더 `X-Requested-With: chongmu` 필수(LARGE API 에만 적용). 없으면 403 `CSRF_HEADER_MISSING`.
- GET 은 절대 상태를 바꾸지 않습니다(참석·승인·변경은 명시적 POST).

### 오류 형식
```json
{"error": {"code": "ALLOCATION_EXCEEDS_TRANSACTION", "message": "사람이 읽는 설명", "detail": {}}}
```
| HTTP | code 예 | 의미 |
|---|---|---|
| 401 | `UNAUTHENTICATED`, `INVITE_INVALID`, `INVITE_EXPIRED`, `INVITE_REVOKED` | 로그인·토큰 문제 |
| 403 | `FORBIDDEN`, `CSRF_HEADER_MISSING` | 권한 없음(담당자 해제 등) |
| 404 | `NOT_FOUND` | 없음 **또는 남의 행사·팀 데이터**(존재 여부를 숨김) |
| 409 | `MODE_LOCKED`, `REFUND_RULE_MISSING`(detail.refundable_max), `RULE_APPLIES`, `NO_CHANGE`, `PENDING_CHANGE_EXISTS`, `ALREADY_DECIDED`, `DUPLICATE_ALLOCATION`, `ALLOCATION_EXCEEDS_TRANSACTION`, `SETTLEMENT_BLOCKED`(detail=사유 목록), `SETTLEMENT_NOT_CONFIRMED`, `PAYOUT_EXCEEDS_REFUND`, `LOCKED_AFTER_SNAPSHOT`, `TASK_NOT_OPEN` | 상태 충돌 |
| 422 | `INVALID_REFUND`, `NOTE_REQUIRED`, `VALIDATION_ERROR`, `INVALID_CHOICE`, `UNDECIDED_AFTER_DEADLINE`, `PARTICIPANT_TEAM_MISMATCH`, `NOT_TEAM_MEMBER`, `CSV_COLUMNS`, `CSV_ROWS`(detail=[{line,error}]), `CSV_EMPTY` | 입력 오류 |
| 429 | `TOO_MANY_ATTEMPTS` | 로그인·인증코드 시도 초과 |
| 503 | `CONCURRENT_UPDATE` | 동시에 다른 쓰기가 있어 저장 실패 → 재시도 |

## 2. 인증
| 메서드·경로 | 입력 | 결과 |
|---|---|---|
| POST `/api/large/auth/admin/login` | `{email, password}` | `cm_admin` 쿠키 |
| GET `/api/large/auth/admin/me` | | `{admin, events[]}` |
| POST `/api/large/auth/invite/exchange` | `{token}` (링크 `#t=` 값) | `cm_participant` 쿠키, `{participant:{id,name}}` |
| POST `/api/large/auth/manager/request-code` | `{login_id}` (명단 ID 또는 Slack ID) | 항상 `{sent:true, message}` (담당자 여부 숨김). 담당자면 Slack DM 으로 6자리 코드 |
| POST `/api/large/auth/manager/verify` | `{login_id, code}` | `cm_manager` 쿠키(team_id 고정) |
| POST `/api/large/auth/{admin|participant|manager}/logout` | | 세션 폐기 |
| GET `/api/large/meta` | | `{app_mode, notify_mode(mock|slack), llm_mode(mock|claude|gemini), demo_tools}` (`email_mode` 는 이전 이름) |

## 3. 참가자 `/api/large/me` (세션의 본인만)
| | |
|---|---|
| GET `/api/large/me` | 본인·팀·행사·현재 응답·회비(부담액)·승인 대기 변경·변경 이력·담당자 여부 |
| POST `/api/large/me/response/preview` `{choice}` | 저장 없이 금액 미리보기 |
| POST `/api/large/me/response` `{choice, note?}` | 마감 전: 즉시 반영(`applied`). 마감 후: `pending_approval` + 총무 과제 |
| GET/POST `/api/large/me/chat` `{text}` | 소통 Agent 대화 (§6) |

`choice ∈ bus | car | absent | undecided`. 마감 후 `undecided` 불가.

## 4. 담당자 `/api/large/team` (세션의 팀만)
| | |
|---|---|
| GET `/api/large/team` | 응답 집계, `billing`(=정산 계산), 팀 변경 이력, 받은 확인 과제, 확정 명세 |
| POST `/api/large/team/tasks/{id}/answer` `{option, note?}` | 과제 답변 → 정산 Agent 즉시 재검토, `{review:{summary}}` |
| POST `/api/large/team/statement/ack` | 확정 명세 '내역 확인' |
| POST `/api/large/team/corrections` `{text}` | 총무에게 정정 요청 과제 |
| GET/POST `/api/large/team/chat` | 소통 Agent(담당자 도구) |

## 5. 총무 `/api/large/admin/events/{event_id}/…` (자기 행사만)
| 영역 | 경로 |
|---|---|
| 대시보드 | GET `` (+ `stage`, `report{version,…}`. 지난 마감의 기준표가 없으면 이때 저장) |
| 설정 | PUT `` (`name,event_at,attend_deadline,payment_deadline,fee_*,account_note`; 스냅샷 후 회비·참석 마감 잠김) · GET/POST `/refund-rules` · DELETE `/refund-rules/{id}` |
| 명단 | POST `/roster` (CSV: **ID,이름,전화번호,Slack ID,소속 부서,팀 담당자** → 부서=팀, 담당자 자동 지정. 결과 `{rows, inserted, updated, teams, managers_assigned, departments[], warnings[]}`) · GET `/participants?team_id&q`(이름·ID·Slack ID) · GET `/teams`(+ `counts`, 담당자 slack_id·phone) |
| 담당자 | POST `/teams/{team_id}/manager` `{participant_id}` (그 팀원만, 기존 담당자 해제) |
| 초대 | POST `/participants/{id}/invite/reissue` (기존 전부 폐기 + 새 초대 DM) · `/invite/revoke` |
| 응답 | POST `/participants/{id}/response` `{choice, note}` (총무 직접 입력·당일 변동. 마감 후면 규칙으로 금액이 정해지는 건 즉시 승인, 규칙이 없는 건(예: 당일 불참)은 승인 대기) · POST `/snapshot` |
| 변경 | GET `/changes?status=` (+ `evidence[]` 슬랙 근거, `suggested_refund`/`suggested_note` 총무 슬랙 결정) · POST `/changes/{id}/approve|reject` `{note, refund_amount?}` — 환불 규칙이 없는 건은 `refund_amount`+`note` 없이는 승인 불가 |
| 입금 | POST `/transactions` (CSV: 거래일시,입금자명,금액,메모) · GET `/transactions` · POST `/transactions/{id}/allocations` `{team_id, participant_id?, amount, basis, confirm}` · POST `/allocations/{id}/confirm|cancel` · POST `/allocations/confirm-high` |
| Agent | POST `/agent/settlement-review` · GET `/agent-runs?agent=` · GET `/agent/timeline`(감지→판단→실행→넘김, 원래 시각) · GET `/agent/settlement-card`(자동 처리율·사람 확인 사유) |
| 과제 | GET `/tasks?status=active|…&assignee=` · POST `/tasks/{id}/resolve` `{resolution}` · POST `/tasks/{id}/accept-suggestion`(슬랙 근거의 배정 후보를 총무가 지정 → 확정 배정 + 그 거래 과제 닫기) |
| 정산 | GET `/settlements/{team_id}` · POST `/settlements/compute` · POST `/settlements/{team_id}/confirm` · POST `/settlements/{team_id}/payouts` `{amount, memo}` |
| 정산 보고서 | GET `/report?since=N`(최신 + `versions[]`, `event`, `diff_base`; since 면 vN 대비 diff) · GET `/report/teams/{team_id}/evidence`(근거 패널) · POST `/report/refresh` · GET `/report.csv` (§14) |
| 근거 메시지 | GET `/evidence/messages?team_id&participant_id` — 슬랙(`demo_slack`) + 보낸 DM(`dm_job`) 시간순 (§16) |
| 총무 Agent | GET/POST `/agent/chat` `{text}` → `{thread, reply}`(메시지마다 `card`) · GET `/agent/briefing`(먼저 하는 보고, 조회만) · POST `/agent/briefing/seen` (§15) |
| 알림(Slack DM) | POST `/emails/enqueue` `{kind: invite|attend_reminder|payment_reminder|refund_notice|extra_notice}` · POST `/emails/preview` `{kind: refund_notice|extra_notice}`(보내지 않고 대상·금액·팀원·문구, `already_sent`) · POST `/emails/dispatch` · GET `/emails` · GET `/emails/sent?kind=`(본문 포함) |
| 감사 | GET `/audit` |

시연 전용(`APP_MODE=demo` 일 때만 등록): GET `/api/large/dev/inbox?to=`, GET `/api/large/dev/people`, POST `/api/large/dev/switch {role, participant_id?}`. 총무 경로 POST `/demo/responses` · `/demo/changes` · `/demo/slack`(운영 모드 404).

## 6. 채팅 상태 계약
`ChatThread(participant_id, role)` 당 하나. 서버에 저장되어 나중에 이어집니다.

| state | 의미 | pending |
|---|---|---|
| `idle` | 대기 | `null` |
| `awaiting_info` | Agent 가 부족한 정보를 물음 | `{intent: "change"|"answer_task", choice?, option?}` |
| `awaiting_confirmation` | 갱신 직전 사용자 확인 대기 | `{tool, args, preview}` |

전이 규칙(코드로 고정)
1. `awaiting_confirmation` 에서 짧은 긍정(“네”, “맞아요”) → **코드가** 저장된 도구 실행 → `idle`.
   단, '네'는 직전 확인 질문(`pending.asked_message_id`)에만 묶입니다. 그 사이 다른 메시지(정산 Agent 의 새 확인 요청 알림, 화면 버튼 답변 등)가 끼면 실행하지 않고 같은 내용을 다시 묻습니다. 과제가 그새 처리됐으면 실행하지 않습니다.
   열린 과제가 여러 개인데 번호 없이 '네'만 오면 어느 과제인지 되묻고(`awaiting_info`), 번호가 올 때까지 아무것도 답하지 않습니다. 부정(“아니요”) → 취소 → `idle`. 다른 말 → 확인 취소 후 새 요청으로 해석.
2. LLM 이 갱신 도구(`request_change`, `answer_task`, `request_correction`)를 고르면 실행하지 않고 **검증(dry run)** 후 확인 질문으로 바꿈. 확인 문구의 금액은 코드가 계산.
3. 조회 도구는 바로 실행해 결과를 LLM 에 돌려줌(한 턴 최대 5단계).

### LLM 행동 계약
```json
{"type":"tool","name":"preview_change","args":{"choice":"car"}}
{"type":"ask","text":"버스·자차·불참·미정 중 무엇으로 바꿀까요?","pending":{"intent":"change"}}
{"type":"reply","text":"현재 응답은 참석+버스이고 회비는 20,000원입니다."}
```

### 도구 계약
| 도구 | 역할 | 입력 | 출력 | 갱신 |
|---|---|---|---|---|
| `get_my_status` | 참가자 | `{}` | `{choice, choice_label, fee, attend_deadline, pending_change…}` | |
| `get_fee_rules` | 참가자·담당자 | `{}` | `{fees, refund_rules[]}` | |
| `preview_change` | 참가자 | `{choice, target?}` | `{from/to, after_deadline, fee_before/after, refund_amount, extra_charge, rule_text}` | |
| `request_change` | 참가자 | `{choice, target?}` | `{change_id, status, refund_amount, extra_charge, rule}` | ✔ |
| `get_team_summary` | 담당자 | `{}` | `{counts, final_fee, received, refunds_paid, balance, blockers}` | |
| `list_open_tasks` | 담당자 | `{}` | `{tasks[{task_id, question, options}]}` | |
| `answer_task` | 담당자 | `{task_id, option, note?}` | `{task_id, status, review}` | ✔ |
| `request_correction` | 담당자 | `{text}` | `{task_id}` | ✔ |

`target` 이 본인이 아니면 `FORBIDDEN`. 다른 팀 과제 ID 는 `NOT_FOUND`. 역할 밖 도구는 `FORBIDDEN`. 도구 오류는 `{ok:false, error:{code,message}}` 로 LLM 에 전달됩니다.

## 7. 데이터 모델 (모든 연결은 ID)
| 테이블 | 핵심 컬럼 | 비고 |
|---|---|---|
| `admins` | email, password_hash | scrypt |
| `events` | admin_id, attend_deadline, payment_deadline, fee_bus/car/absent, snapshot_taken_at | |
| `teams` | event_id, code, name | 팀 = 부서(명단의 소속 부서). code 는 처음 나온 순서로 T01… |
| `participants` | event_id, team_id, employee_no(명단 ID), name, slack_id, phone | (event, slack_id)·(event, employee_no) 유일 |
| `team_managers` | team_id, participant_id, assigned_by, revoked_at | 팀당 활성 1명(부분 유니크) |
| `invites` | participant_id, token_hash, expires_at, revoked_at | 원문 미저장 |
| `sessions` | token_hash, role, participant_id/team_id/admin_id, via, expires_at | |
| `manager_codes` | participant_id, code_hash, attempts, expires_at | |
| `responses` | participant_id(유일), choice | 현재 응답. 행 없음 = 미응답 |
| `deadline_snapshots` | event_id, participant_id, choice, fee | 마감 기준 응답, 수정 안 함 |
| `change_logs` | from/to, after_deadline, status, liability_before/after, refund_amount, extra_charge, refund_rule_id, rule_text, evidence_message_id | 변경 이력 |
| `refund_rules` | change_type, valid_until, refund_percent | |
| `uploads` / `bank_transactions` | row_hash(행사 내 유일) | 원본 입금, 수정 안 함 |
| `allocations` | transaction_id, team_id, participant_id?, amount, status(proposed/confirmed/cancelled), source, confidence, basis | (거래, 팀, 개인) 활성 유일 |
| `confirm_tasks` | kind, assignee, team/participant/transaction/change id, status, question, options, answer, evidence, rule, dedupe_key(유일) | 확인 과제 |
| `chat_threads` / `chat_messages` | state, pending / sender, text, meta(steps, rules) | |
| `agent_runs` | agent, trigger, evidence_message_id, steps, rules, result, llm_mode | 실행 근거 |
| `email_jobs` / `dev_inbox` | kind, to_email(=받는 사람 Slack ID), dedupe_key, status, attempts, scheduled_at, note | Slack DM 작업(이름은 호환용). 본문은 발송 시 렌더(토큰 미저장) |
| `team_billings` | team_id(유일), billed, counts, unresolved, lines[{participant_id, choice, fee}], received_by_deadline, captured_at | **납부 마감 청구표**, 한 번 저장 후 수정 안 함 |
| `settlement_reports` | version, trigger, stage, content_hash, summary, teams[], people[], diff | 정산 보고서 버전. 내용이 같으면 새 버전 없음 |
| `admin_chats` / `admin_chat_messages` | state, pending / sender, text, meta(steps, llm, report_version) | 총무 Agent 대화(행사당 1개) |
| `settlements` / `payouts` | version, status, final_fee, received, refunds_paid, balance, detail, blockers / amount | |
| `audit_logs` | actor, action, target, detail | |

## 8. 확인 과제 종류
`change_approval`(총무) · `unknown_depositor` · `ambiguous_depositor` · `unclear_deposit` · `possible_duplicate` · `unresolved_response`(담당자, 정산 차단 안 함) · `correction_request` · `deposit_unresolved` · `overpayment_unexplained` · `multiple_team_reference`(총무 이관). 입금자 확인 과제의 `evidence.slack_suggestion{team_id, team, message_id, sender, sent_at, text, why}` = 정산 Agent R7(슬랙 입금 단서, 배정은 총무 지정).
상태: `open → answered → resolved` 또는 `escalated → resolved(총무)`.

## 9. 알림 작업 (Slack DM, `NOTIFY_MODE=mock|slack`)
| kind | 대상 | dedupe_key | 발송 직전 재검사 |
|---|---|---|---|
| `invite` | 전원 | `invite:{pid}:initial` / `:reissue:{ts}` | — (발송 시 새 토큰 발급) |
| `attend_reminder` | 미응답·미정 개인 | `attend:{pid}:{KST날짜}` | 그새 응답했으면 skip |
| `payment_reminder` | 잔액>0 팀의 담당자 | `payment:{team}:{KST날짜}` | 담당자 변경·잔액 0·입금 확인 과제 진행 중이면 skip |
| `confirm_request` | 과제 받은 담당자 | `confirm:{task}` | 과제가 이미 처리됐으면 skip |
| `manager_code` | 인증 요청자 | `manager_code:{id}` | 즉시 발송(금지 시간·한도 예외) |
| `refund_notice` | 잔액<0(환불) 부서의 **담당자만** | `refund_notice:{team}:{잔액}` | 잔액이 바뀌어 환불이 아니면 skip. 확인 필요 부서는 만들지 않음 |
| `extra_notice` | 잔액>0(추가 납부) 부서의 **담당자만** | `extra_notice:{team}:{잔액}` | 같음 |

발송: `conversations.open(users=Slack ID)` → `chat.postMessage`. 모의 모드에서 Slack ID 에 `BOUNCE` 가 있으면 실패 재현.

상태: `queued → sent | skipped | retry_wait(5·10분 백오프) → failed(3회)`. 금지 시간(21~8시 KST)·수신자별 하루 2건 초과 시 `scheduled_at` 연기.

## 10. 환불 규칙이 없는 변경(임시 0원)
- 요청 시각에 맞는 `RefundRule` 이 없으면 `change_logs.rule_missing=1`, 환불액은 **미정**(값은 임시 0). 참가자 확인 문구·총무 과제에 '미정'으로 표시.
- 총무 승인 시 ① 규칙을 추가하고 승인 → 규칙으로 계산, 또는 ② `refund_amount`(0~환불 대상액)와 사유 `note` 를 직접 입력 → `admin_refund_decided=1`. 둘 다 없으면 `409 REFUND_RULE_MISSING`.
- 승인 전에는 `pending_approval`·열린 과제로 정산 확정이 막히고, 혹시 결정 없이 반영된 이전 데이터가 있으면 정산 blocker("임시 0원으로 남은 변경")로 막힙니다.

## 12. 이메일 발송 스케줄러
- GET `/api/large/admin/events/{id}/emails/scheduler` → `{configured, running, paused, enabled, interval_seconds, runs, last_run_at, last_result, last_error, email_mode}`
- POST 같은 경로 `{enabled: bool}` → 이 프로세스의 일시정지·재개. 설정으로 꺼진 채 시작했으면 `409 SCHEDULER_NOT_STARTED`
- 이메일 작업 상태에 `sending`(선점됨)이 추가. `claimed_at`, `claimed_by`(호스트:PID:manual|scheduler) 기록. 10분 넘게 `sending` 이면 `failed`+확인 메모(자동 재발송 없음)
- `/api/large/meta.scheduler = {enabled, interval_seconds}`

## 13. Agent 실행 기록의 판단 주체
`agent_runs.result.decisions[] = {by, action, after_steps}` — `by`: `claude:<모델>`·`gemini:<모델>`(실제 LLM), `mock`(규칙 기반 설정), `mock_fallback`(LLM 실패·형식 오류 후 대체), `code`('네/아니요' 판정). `steps[]` 는 코드가 실행한 도구와 결과. `result.llm.fallback` 에 대체 사유. 채팅 메시지 `llm` 필드에 같은 요약. 정산 처리 실행(`agent='settlement'`)은 `llm_mode='rule'` — LLM 을 쓰지 않음.

## 11. 공용 서버(hub)와의 경계
- LARGE 는 `app.main.configure_large(app)`(API·오류 처리·CSRF 미들웨어)와 `add_large_ui(app)`(`/large/` 정적 화면, 내부 경로 새로고침 시 index.html)를 내보냅니다. hub 가 이 둘을 붙입니다.
- `/api/large/*` 의 없는 경로는 LARGE 가 404 로 끝냅니다(SMALL 로 넘어가지 않음).
- LARGE DB(`large/backend/var/chongmu.db`)와 SMALL 데이터(`app/data/`, 브라우저 localStorage)는 공유하지 않습니다.

## 14. 마감 기준표와 정산 보고서
- 기준표(한 번 저장, 수정 안 함): 참석 마감 `deadline_snapshots`(사람별) · 납부 마감 `team_billings`(부서별 청구표). 마감이 지나면 스케줄러·대시보드·보고서 조회 때 빠진 것을 저장합니다. 청구표는 `as_of=납부 마감`으로 계산해 언제 저장해도 같은 값입니다.
- 변동 기록(추가만): `change_logs`(마감 후·당일 변동), `bank_transactions`+`allocations`, `payouts`.
- 보고서 `settlement_reports`: 기준표+변동 기록을 정산 Agent 가 다시 계산. 돈·인원에 영향을 주는 POST(변경 승인·반려, 거래 업로드, 배정 확정·취소, 과제 처리, 담당자 답변, 정산 확정, 송금 기록, 명단 업로드, 총무 직접 입력, 총무 Agent 질문) 뒤에 `refresh(trigger)` → `content_hash` 가 같으면 버전 유지, 다르면 `version+1` + `diff{teams[], people_added[], people_removed[]}` + `agent_runs(agent='settlement', trigger='report:…')`.
- 부서 행: `billed, final_fee, received, received_proposed, paid_by_deadline, late_paid, refunds_paid, payment_gap(=billed−received), change_delta(=final−billed), balance, status(extra|refund|check|done), blockers[]`. `balance = payment_gap + change_delta + refunds_paid`.
- 사람 행: 참석 마감 이후 모든 변동. `kind ∈ extra|refund|pending|unresolved|rule_missing|changed|included`(included = 납부 마감 전에 승인돼 청구에 이미 포함), `amount`, `on_event_day`(행사 날짜 KST 0시 이후), `basis_kind(rule|admin|pending)`·`basis`, `slack{at,sender,text}`(가장 최근 관련 메시지), `process`(팀 납부 완료(날짜)/추가 납부 대기/환불 대기/승인 대기/…), `changes[]`(from/to·source·requested_at·decided_at·rule·note).
- summary 추가: `deadline_counts`(참석 마감 기준 인원), `billed_final_delta`(최종−청구).
- `stage`: `collecting`(참석 마감 전) → `attend_closed` → `payment_closed` → `event_done`(행사 시작 후).

## 15. 총무 Agent
- 매 질문마다 보고서를 `refresh` 한 뒤 그 버전으로 답합니다. 숫자는 보고서에서만(코드 계산).
- 조회 도구: `get_report_summary{}` · `get_department{team}` · `list_people{kind?, team?, on_event_day?}` · `list_teams{status?}` · `find_person{name}` · `what_changed{}` · `get_evidence{team?|person?}`(청구→최종 변동 줄·규칙/총무 결정·요청/승인 일시·슬랙 메시지, 입금, 확인 필요). `team` 은 부서 이름·일부·팀코드·'담당자 이름 + 팀'.
- 답변 메시지의 `tools[]` 는 이번 턴에 부른 도구 순서(화면: `get_department → get_evidence`).
- **보고 카드** `card`(agents/admin_cards.py): `{type: team|teams|people|person|summary|confirm|result|briefing, greeting, headline, status{key,label}, story[{at, who, text, quote}], money{billed, final, diff, if_approved?, received?, balance?, show_received, total?}, rows[], first?, todo[{label, action, target_id?, team?, kind?}], sources{slack, changes, receipts, team_id}, tools[]}`.
  숫자는 코드 계산. mock 은 템플릿, claude/gemini 는 `reply.card{greeting, headline, story}` 문장만 받아 금액이 도구 결과에 없으면 버리고 템플릿(`llm_note`).
  승인 대기·입금 대조 전이면 headline 에 그 사실, 거래내역 전에는 `received/balance` 를 넣지 않는다. `todo.action`: approve_change·accept_suggestion·confirm_allocation(화면에서 한 번 더 확인 후 실행) / open_changes·open_task·open_payments·open_roster·open_evidence(이동) / notify_preview(채팅 '네' 확인 흐름).
- **행동 뒤 결과 보고**: 변경 승인·반려, 배정 확정·일괄 확정, 정산 Agent 점검, 슬랙 근거 지정 응답에 `agent_report`(같은 카드가 총무 채팅에도 남음) — "승인했어요. ○○팀 최종 회비가 …원이 됐고 보고서 vN 에 반영했어요. … 안내를 보낼까요?"
- 갱신 도구: `notify_managers{kind: refund|extra, team?}` — dry run 으로 대상·금액을 보여 주고 `awaiting_confirmation`. '네/아니요'는 코드가 판정. 실행 시 `refund_notice/extra_notice` 작업 생성 + 발송. 담당자에게만.
- LLM: `LLM_MODE=mock`(규칙 기반 해석) | `claude`(루트 `.env` 의 `LLM_API_KEY`) | `gemini`. 실패하면 규칙 기반 대체, `decisions[].by` 에 기록.

## 16. 슬랙 대화 근거(`evidence_messages`, 시연용)
- POST `/demo/slack` (CSV 열: **일시, 보낸 사람, 받는 사람, 부서, 관련 ID(비어도 됨), 내용**). 부서·ID 가 명단에 없거나 ID 가 그 부서 소속이 아니면 줄 번호와 함께 전체 반려(422 `CSV_ROWS`). 넣을 때마다 그 행사의 `demo_slack` 행과 거기서 나온 미승인 제안을 지우고 다시 넣는다(멱등).
- 정산 Agent 해석(`analysis`): `kind ∈ change|admin_refund|deposit_hint|payment_promise|bot_dm|admin_note|note`, `to`, `confidence(high|medium|low)`, `by(rule|claude:모델|gemini:모델|mock_fallback)`, `outcome ∈ linked|proposed|no_change|conflict|suggest_refund|before_deadline|rejected|deposit_hint|recorded`, `change_id`.
- `LLM_MODE=claude|gemini` 면 LLM 이 JSON(`items[{message_id, kind, employee_no, from, to, at, amount, depositor, channel, confidence}]`)으로 추출 → 코드가 명단·관련 ID·현재 응답과 대조해 통과한 것만 사용. 실패하면 규칙 추출(`mock_fallback`).
- 변경: 이미 같은 변경 기록이 있으면 연결(`linked`), 없으면 `submit_change(source='slack', 요청 시각=메시지 시각)` → **승인 대기 제안**(자동 확정 없음). 금액은 요청 시각의 환불 규칙으로 코드가 계산.
- 응답·변경 CSV 를 다시 넣으면 메시지를 새 기록에 다시 잇는다. 보낸 DM(`email_jobs` sent)은 저장하지 않고 조회 때 `source='dm_job'` 으로 합친다.

## 17. 슬랙 실시간 연동 `POST /api/large/slack/events`
- 한 메시지 = `services.slack.ingest_slack_message(event, sender_slack_id|sender, channel, text, ts, …)` → 근거 저장 → 정산 Agent 해석 → 대조 → 연결/제안·입금자 근거 → Agent 실행 기록. 슬랙 대화 CSV 업로드(§16)는 이 함수를 행마다 원래 시각으로 부르는 **재생기**.
- Slack Events API 형식: `url_verification` → `{challenge}`. `event_callback` 의 `message`(DM `message.im`·채널)만, `subtype`·`bot_id` 무시. 보낸 사람 Slack ID 로 대모임 명단 매칭(없으면 200 `ignored`). 같은 `event_id` 는 한 번만(`duplicate`). 메시지 속 같은 부서 팀원 'OO님'이 한 명이면 그 사람의 변경, 아니면 보낸 사람 본인.
- 보안: **서명 검증 필수** — `X-Slack-Signature == "v0=" + HMAC_SHA256(SLACK_SIGNING_SECRET, "v0:{X-Slack-Request-Timestamp}:{body}")`, 5분 넘은 요청 거부. 비밀값이 없거나 틀리면 403. 쿠키를 쓰지 않으므로 이 경로만 `X-Requested-With` 검사에서 빠진다.
- 설정 키(기본 꺼짐): `SLACK_EVENTS=on`, `SLACK_SIGNING_SECRET=…`(backend/.env 또는 루트 .env). Slack 앱 설정: Event Subscriptions → Request URL `https://<공개 주소>/api/large/slack/events`, Bot events `message.im`(필요하면 `message.channels`), 스코프 `im:history`(`channels:history`). 공용 서버(hub)·소모임 코드는 바꿀 필요 없음(`/api/large/*` 는 이미 대모임으로 연결).

## 18. 대시보드 작업대·환불 계좌·정산 CSV
- GET `/workbench` → `{teams[{team_id, name, manager, status, balance, final_fee, billed, received, todo[], refund_state}], orphan_tasks[], refunds, first_team_id, urgent}`. 정렬: 확인 필요 → 추가 → 환불 → 완료(같으면 할 일 많은 순). `todo` = 변경 승인 n · 입금 제안 확정 n · 열린 확인 과제 · 응답 미확정 n · (안내 안 보낸 추가/환불) 담당자 안내 · (안내 뒤) 계좌 입력 대기/계좌 확인/환불 송금. `first_team_id` = 브리핑 `urgent.team_id`(브리핑 후보마다 `team_id` 가 붙음).
- GET `/report/teams/{id}/evidence` 에 추가: `changes[].rule_missing/rule_text/suggested_refund/suggested_note`(승인 칸 미리 채움), `members[{snapshot_choice, choice, fee, delta, pending, changes}]`(마감 기준 vs 현재), `tasks[]`(과제 요약 + 슬랙 배정 후보 + 거래), `refund`(아래 환불 송금 행), `notices{refund_notice|extra_notice: {status, sent_at, current}}`.
- 화면 색(마감 기준 ≠ 현재): 현재 칩 추가=주황(`--orange`), 환불·감액=파랑(`--blue`), 승인 대기=노랑 점선(`--yellow`) + 행 왼쪽 막대. 마감 후 변경 금액은 칩(+5,000 / −10,000).
- POST `/tasks/{id}/resolve` 응답에 `agent_report`. POST `/emails/enqueue` 에 `team_ids`(환불·추가 납부 안내를 그 부서에만).
- 정산 CSV(UTF-8 BOM): GET `/report.csv?kind=teams` 부서별 정산표(부서, 담당자, 청구, 변동 합계, 최종, 입금, 잔액, 상태, 근거 요약) · `kind=people` 사람별 변동표(부서, 이름, 마감 기준, 현재, 금액, 근거, 처리 상태) · kind 없음 = 기존 보고서 전체.
- 환불 계좌 `refund_accounts(team_id, bank, account_no, holder, status candidate|saved, source manager|link|slack, submitted_by, message_id, submitted_at, confirmed_by, confirmed_at)`, 개인 링크 `refund_links(token_hash, team_id, participant_id, expires_at 7일, used_at)`.
  - 환불 안내 DM(담당자에게만)에 `/large/refund-account#t=토큰` 링크(보낼 때 발급, 미리보기에는 자리 표시만). 공개 API: POST `/api/large/refund-link/open {token}` · `/submit {token, bank, account_no, holder}`(만료·폐기·담당자 변경 시 401).
  - 담당자 화면: GET/POST `/api/large/team/refund-account`(세션의 팀만). 형식 검사: 은행 2~20자, 계좌번호 숫자·하이픈만 숫자 10~16자리, 예금주 2~10자 → 422 `ACCOUNT_INVALID` + 칸별 `detail[{field, error}]`.
  - 슬랙 답장: 부서 담당자가 '은행 계좌번호 (예금주)'를 보내면(은행 이름이 앞에 있어야 함 — 전화번호 무시) `analysis.kind='refund_account'`, `outcome='account_candidate'` → 후보. 근거 메시지·Agent 기록에는 뒤 4자리만, LLM 에 보내지 않음. 총무 POST `/refunds/{team}/account/{id}/confirm` 으로 저장.
  - 총무: GET `/refunds` → 행 `{team, manager, refund, due, paid, state none|candidate|saved|paid, account·candidate(뒤 4자리만), notice_sent_at, link_expires_at, can_pay, can_delete}` · POST `/refunds/{team}/account/reveal {purpose: view|copy}`(전체 번호, 감사 로그에는 번호 없이) · POST `/refunds/{team}/paid`(정산 확정 + 남은 환불액 지급 기록 → 잔액 0, `agent_report`; 계좌 없음·확인 필요 남음·이미 0원이면 409) · DELETE `/refunds/{team}/account`(잔액 0·확인 필요 없음일 때만) · GET `/refunds/payouts.csv?confirm=yes`(전체 계좌번호가 들어가는 유일한 파일, confirm 없으면 422).
  - 실제 송금은 하지 않는다(총무가 은행 앱에서 보낸 뒤 표시만). 시연 모드 화면·DM 에 "실제 계좌는 넣지 마세요(예: 시연은행 000-0000-0001)".
