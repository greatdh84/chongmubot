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

## 협업 규칙

### 담당 영역 (demo/index.html 한 파일을 나눠 씀)

| 담당 | 고치는 곳 |
|---|---|
| 채총 | `LARGE` 블록 (`/* 대모임 — 채총 영역 */` ~ `/* 대모임 블록 끝 */`), 대모임 전용 `<style>` |
| 도총 | 그 밖의 전부 (공용 상태·화면·대시보드, 소모임 설정) |

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
