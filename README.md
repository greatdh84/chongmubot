# 총무봇 (도총 & 채총)

모임 총무의 회비 수납·대조·독촉·정산을 AI 에이전트(기록·정산·소통)가 맡고, 총무는 확인이 필요한 몇 건만 결정하는 앱입니다.
서울대 KDT Hands on AI Bootcamp · Category C 과제.

## 구조

```
demo/   대시보드 데모 (HTML, 서버 없음)
app/    실제 앱: Slack 봇, 시트 연동, 에이전트
docs/   기획안, flowchart, 회의 메모
```

화면 흐름: 시작하기 → 모임 규모 선택(대모임 / 소모임) → 설정 → 대시보드

## 데모 실행

`demo/index.html` 을 브라우저로 엽니다. 온라인: https://greatdh84.github.io/chongmubot/demo/

## Slack 연동 (로컬)

> ⚠️ 수업 워크스페이스(약 380명)에 연결됩니다. 시작하기를 누르면 채널이 실제로 만들어지고 초대 알림이 가므로, 테스트할 땐 본인·채총 등 소수만 고르세요.

서버가 꺼져 있으면 데모 멤버로 동작하고, 켜져 있으면 설정 화면의 "워크스페이스 멤버"가 실제 Slack 멤버로 바뀌며 시작 시 채널을 실제로 만들고 초대합니다.

1. https://api.slack.com/apps → Create New App → From a manifest → 워크스페이스 선택 → [app/slack-manifest.json](app/slack-manifest.json) 붙여넣기
2. OAuth & Permissions → Bot Token Scopes: `users:read`, `channels:manage`, `channels:read` → Install to Workspace
3. `.env` 에 `SLACK_BOT_TOKEN=xoxb-…`, `SLACK_OWNER_USER_ID=U…` (Slack 내 프로필 → ⋮ → 멤버 ID 복사)
4. `python3 app/server.py` (설치할 패키지 없음, http://127.0.0.1:8787)
5. `demo/index.html` 을 열면 자동 연결. 다른 주소는 "Slack 연결" 버튼이나 `?api=` 로 지정

채널 이름은 공백·마침표·대문자 없이 80자 이하여야 합니다.

## 협업 규칙

### 담당 영역 (demo/index.html 한 파일을 나눠 씀)

| 담당 | 고치는 곳 |
|---|---|
| 도총 | `SMALL` 블록 (`/* 소모임 — 도총 영역 */` ~ `/* 소모임 블록 끝 */`), 기타 설정 (공용 상태·시작화면·규모 선택·대시보드) |
| 채총 | `LARGE` 블록 (`/* 대모임 — 채총 영역 */` ~ `/* 대모임 블록 끝 */`), 대모임 전용 `<style>` |

같은 파일이어도 서로 다른 줄을 고치면 Git이 자동으로 합칩니다. 상대 영역을 고쳐야 하면 GitHub Issue로 요청합니다.

### 대모임 블록 쓰는 법

- 상태: `S.large` 안에만 저장. 처음 값은 `LARGE.defaults()`
- 입력칸: `data-bind="large.항목"` → 자동 저장
- 버튼: `data-act="large:이름"` → `LARGE.act.이름(d, el)` 실행 후 화면 갱신 (`false`를 돌려주면 갱신 안 함)
- 화면: `LARGE.render(el)`
- 스타일: 클래스는 `.lg-` 로 시작

### 브랜치와 PR

| 브랜치 | 담당 | 용도 |
|---|---|---|
| `main` | 공동 | 항상 열리는 상태. 직접 커밋하지 않고 PR로만 머지 |
| `feat/small` | 도총 | 소모임·공용 부분 작업 (계속 사용) |
| `feat/large` | 채총 | 대모임 작업 (계속 사용) |
| `feat/slack` | 도총 | Slack 봇 (`app/`). 개인 테스트 워크스페이스에서 개발 (계속 사용) |
| `docs/…` | 공동 | 문서 작업 (머지 후 삭제) |

- 작업 흐름: 자기 브랜치에서 커밋·Push → 기능 하나가 끝나면 `main` 으로 PR → 머지
- 브랜치를 계속 쓰므로, 상대 PR이 머지되면 자기 브랜치에 `main` 을 합칩니다: `git switch feat/small` → `git pull origin main`
- 커밋 메시지: 무엇을 했는지 한국어 한 줄
- 상대 영역을 건드린 PR은 상대가 확인 후 머지, 자기 영역만 바꾼 PR은 본인이 머지

### 비밀값

Slack 토큰, Google 서비스 계정 JSON, LLM API 키는 `.env` 에만 넣습니다 (`.env.example` 참고). 실수로 올렸으면 즉시 키를 새로 발급합니다.

전체 방침: [docs/GitHub_운용방침.pdf](docs/GitHub_운용방침.pdf) (파일 분리 전제로 작성됨 — 담당 영역은 이 README 기준)

## 일정

- 제출 2026-10-13 · 발표 2026-10-15
