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
| 422 | `INVALID_REFUND`, `NOTE_REQUIRED`, `VALIDATION_ERROR`, `INVALID_CHOICE`, `UNDECIDED_AFTER_DEADLINE`, `PARTICIPANT_TEAM_MISMATCH`, `NOT_TEAM_MEMBER`, `CSV_COLUMNS`, `CSV_ROWS` | 입력 오류 |
| 429 | `TOO_MANY_ATTEMPTS` | 로그인·인증코드 시도 초과 |
| 503 | `CONCURRENT_UPDATE` | 동시에 다른 쓰기가 있어 저장 실패 → 재시도 |

## 2. 인증
| 메서드·경로 | 입력 | 결과 |
|---|---|---|
| POST `/api/large/auth/admin/login` | `{email, password}` | `cm_admin` 쿠키 |
| GET `/api/large/auth/admin/me` | | `{admin, events[]}` |
| POST `/api/large/auth/invite/exchange` | `{token}` (링크 `#t=` 값) | `cm_participant` 쿠키, `{participant:{id,name}}` |
| POST `/api/large/auth/manager/request-code` | `{email}` | 항상 `{sent:true, message}` (존재 여부 숨김) |
| POST `/api/large/auth/manager/verify` | `{email, code}` | `cm_manager` 쿠키(team_id 고정) |
| POST `/api/large/auth/{admin|participant|manager}/logout` | | 세션 폐기 |
| GET `/api/large/meta` | | `{app_mode, email_mode, llm_mode, demo_tools}` |

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
| 대시보드 | GET `` |
| 설정 | PUT `` (`name,event_at,attend_deadline,payment_deadline,fee_*,account_note`; 스냅샷 후 회비·참석 마감 잠김) · GET/POST `/refund-rules` · DELETE `/refund-rules/{id}` |
| 명단 | POST `/roster` (CSV: 사번,이름,이메일,팀코드,팀명) · GET `/participants?team_id&q` · GET `/teams` |
| 담당자 | POST `/teams/{team_id}/manager` `{participant_id}` (그 팀원만, 기존 담당자 해제) |
| 초대 | POST `/participants/{id}/invite/reissue` (기존 전부 폐기 + 새 메일) · `/invite/revoke` |
| 응답 | POST `/participants/{id}/response` `{choice}` (총무 직접 입력, 마감 후면 즉시 승인) · POST `/snapshot` |
| 변경 | GET `/changes?status=` · POST `/changes/{id}/approve|reject` `{note, refund_amount?}` — 환불 규칙이 없는 건은 `refund_amount`+`note` 없이는 승인 불가 |
| 입금 | POST `/transactions` (CSV: 거래일시,입금자명,금액,메모) · GET `/transactions` · POST `/transactions/{id}/allocations` `{team_id, participant_id?, amount, basis, confirm}` · POST `/allocations/{id}/confirm|cancel` · POST `/allocations/confirm-high` |
| Agent | POST `/agent/settlement-review` · GET `/agent-runs?agent=` |
| 과제 | GET `/tasks?status=active|…&assignee=` · POST `/tasks/{id}/resolve` `{resolution}` |
| 정산 | GET `/settlements/{team_id}` · POST `/settlements/compute` · POST `/settlements/{team_id}/confirm` · POST `/settlements/{team_id}/payouts` `{amount, memo}` |
| 이메일 | POST `/emails/enqueue` `{kind: invite|attend_reminder|payment_reminder}` · POST `/emails/dispatch` · GET `/emails` |
| 감사 | GET `/audit` |

시연 전용(`APP_MODE=demo` 일 때만 등록): GET `/api/large/dev/inbox?to=`, GET `/api/large/dev/people`, POST `/api/large/dev/switch {role, participant_id?}`.

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
| `teams` | event_id, code, name | |
| `participants` | event_id, team_id, employee_no, name, email | (event, email) 유일 |
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
| `email_jobs` / `dev_inbox` | kind, dedupe_key, status, attempts, scheduled_at, note | 본문은 발송 시 렌더(토큰 미저장) |
| `settlements` / `payouts` | version, status, final_fee, received, refunds_paid, balance, detail, blockers / amount | |
| `audit_logs` | actor, action, target, detail | |

## 8. 확인 과제 종류
`change_approval`(총무) · `unknown_depositor` · `ambiguous_depositor` · `unclear_deposit` · `possible_duplicate` · `unresolved_response`(담당자, 정산 차단 안 함) · `correction_request` · `deposit_unresolved` · `overpayment_unexplained` · `multiple_team_reference`(총무 이관).
상태: `open → answered → resolved` 또는 `escalated → resolved(총무)`.

## 9. 이메일 작업
| kind | 대상 | dedupe_key | 발송 직전 재검사 |
|---|---|---|---|
| `invite` | 전원 | `invite:{pid}:initial` / `:reissue:{ts}` | — (발송 시 새 토큰 발급) |
| `attend_reminder` | 미응답·미정 개인 | `attend:{pid}:{KST날짜}` | 그새 응답했으면 skip |
| `payment_reminder` | 잔액>0 팀의 담당자 | `payment:{team}:{KST날짜}` | 담당자 변경·잔액 0·입금 확인 과제 진행 중이면 skip |
| `confirm_request` | 과제 받은 담당자 | `confirm:{task}` | 과제가 이미 처리됐으면 skip |
| `manager_code` | 인증 요청자 | `manager_code:{id}` | 즉시 발송(금지 시간·한도 예외) |

상태: `queued → sent | skipped | retry_wait(5·10분 백오프) → failed(3회)`. 금지 시간(21~8시 KST)·수신자별 하루 2건 초과 시 `scheduled_at` 연기.

## 10. 환불 규칙이 없는 변경(임시 0원)
- 요청 시각에 맞는 `RefundRule` 이 없으면 `change_logs.rule_missing=1`, 환불액은 **미정**(값은 임시 0). 참가자 확인 문구·총무 과제에 '미정'으로 표시.
- 총무 승인 시 ① 규칙을 추가하고 승인 → 규칙으로 계산, 또는 ② `refund_amount`(0~환불 대상액)와 사유 `note` 를 직접 입력 → `admin_refund_decided=1`. 둘 다 없으면 `409 REFUND_RULE_MISSING`.
- 승인 전에는 `pending_approval`·열린 과제로 정산 확정이 막히고, 혹시 결정 없이 반영된 이전 데이터가 있으면 정산 blocker("임시 0원으로 남은 변경")로 막힙니다.

## 11. 공용 서버(hub)와의 경계
- LARGE 는 `app.main.configure_large(app)`(API·오류 처리·CSRF 미들웨어)와 `add_large_ui(app)`(`/large/` 정적 화면, 내부 경로 새로고침 시 index.html)를 내보냅니다. hub 가 이 둘을 붙입니다.
- `/api/large/*` 의 없는 경로는 LARGE 가 404 로 끝냅니다(SMALL 로 넘어가지 않음).
- LARGE DB(`large/backend/var/chongmu.db`)와 SMALL 데이터(`app/data/`, 브라우저 localStorage)는 공유하지 않습니다.
