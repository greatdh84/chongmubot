# AGENTS.md — 작업 규칙 (사람·코딩 에이전트 공통)

## 먼저 읽을 것
`docs/PRD.md`(범위·규칙) → `docs/api-contract.md`(계약) → `docs/workflows.md`(흐름·HITL).

## 실행·검증
```bash
cd backend && .venv/bin/python -m app.seed --reset && .venv/bin/uvicorn app.main:app --port 8000
cd frontend && npm run dev
# 커밋 전 반드시
cd backend && .venv/bin/python -m pytest -q
cd frontend && npm run typecheck && npm run build
```

## 담당 경계 (2인 팀 제안 — 팀이 확정해서 고칠 것)
| 영역 | 담당 | 주요 파일 |
|---|---|---|
| A: 워크플로·Agent·발표 | 팀원 A | `docs/workflows.md`, `backend/app/agents/`, `backend/app/adapters/llm.py`, 시드 시나리오 |
| B: 화면·API·인증·이메일 | 팀원 B | `frontend/src/`, `backend/app/routers/`, `backend/app/services/emails.py`, `security.py` |
| 공통(둘 다 리뷰) | | `models.py`, `services/fees.py`, `services/payments.py`, `docs/api-contract.md` |

계약(`api-contract.md`)을 바꾸는 PR 은 상대방 리뷰를 받습니다. 각자 브랜치에서 작업하고 main 은 리뷰 후 병합합니다.

## 지켜야 할 규칙
1. **금액 계산·접근 통제·중복 방지는 코드로.** LLM 출력의 숫자를 저장하거나 그것으로 판단하지 않습니다. 금액은 `services/fees.py`·`settlement.py` 에서만 계산합니다.
2. **갱신은 확인 후에만.** 새 갱신 도구는 `ToolSpec(mutating=True, dry_run=…)` 로 등록해 사용자 확인을 거치게 합니다.
3. **권한은 세션 기준.** 참가자·담당자 API 는 경로에서 ID 를 받지 않고 세션의 participant_id/team_id 를 씁니다. 총무 API 는 `admin_event` 의존성 + 하위 리소스 `event_id` 재확인. 남의 데이터는 404.
4. **GET 은 상태를 바꾸지 않습니다.** 링크 열기만으로 참석·승인·변경 처리 금지.
5. **토큰·링크를 로그·DB 평문·응답에 남기지 않습니다.** 토큰은 `hash_token` 만 저장, 메일 본문은 발송 시 렌더링.
6. **금액이 같다는 이유만으로 팀·원인을 확정하지 않습니다.** 근거(팀코드, 이름, 담당자 답변)를 `basis`/`evidence`/`rule` 에 남깁니다.
7. **미응답 ≠ 불참.** 회비 `None`, 정산 차단.
8. **마감 기준 응답은 수정 금지**, 이후 변경은 `ChangeLog` 추가만.
9. 정산에는 `confirmed` 배정만. Agent 는 `proposed` 까지만 만듭니다.
10. 새 규칙·과제 종류를 넣으면 테스트와 `api-contract.md` 표를 같이 고칩니다.

## Git
- 커밋 금지: `.env`, `backend/var/`(로컬 DB·생성 CSV), 실제 명단·거래내역, `node_modules/`, `dist/`.
- `.env.example` 에는 변수 이름만.
- 실제 이메일 발송(`EMAIL_MODE=smtp`)·배포는 팀 합의 후에만.

## 코드 스타일
- 백엔드: 서비스 함수는 커밋하지 않고 라우터가 `db.commit()`. 시각은 `clock.now()`(테스트에서 `clock.freeze`). 사용자에게 보이는 문구는 한국어.
- 프런트: `api.ts` 래퍼만 사용(헤더 자동 첨부), 금액 표시는 `won()`.
