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

`demo/index.html` 을 브라우저로 엽니다.

## 협업 규칙

- `main` 에 직접 커밋하지 않고, `feat/…` 브랜치 → PR → 머지
- 담당 파일은 `.github/CODEOWNERS` 참고
- API 키는 `.env` 에만 (`.env.example` 참고)

자세한 내용: [docs/GitHub_운용방침.pdf](docs/GitHub_운용방침.pdf)

## 일정

- 제출 2026-10-13 · 발표 2026-10-15
