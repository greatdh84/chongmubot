"""대시보드 작업대(부서 목록·첫 선택·부서 상세 할 일), 환불 계좌(담당자 입력·개인 링크·슬랙 후보)·송금 완료, 정산 CSV 내보내기."""
from datetime import timedelta

from sqlalchemy import select

from app import clock
from app.models import AuditLog, EvidenceMessage, Participant, RefundAccount, Team
from app.services import refunds

from .test_slack_events import events_on, msg_event, signed  # noqa: F401  (fixture 재사용)
from .test_slack_evidence import EVENT_AT, changes_csv, demo, slack_csv, transactions_csv, up  # noqa: F401


def _settle(client, base):
    up(client, f"{base}/demo/changes", changes_csv())
    up(client, f"{base}/demo/slack", slack_csv())
    up(client, f"{base}/transactions", transactions_csv())
    client.post(f"{base}/agent/settlement-review")
    client.post(f"{base}/allocations/confirm-high")


def _team(client, base, name):
    return next(t for t in client.get(f"{base}/report").json()["teams"] if t["name"] == name)


def test_parse_account_and_mask():
    p = refunds.parse_account("국민 123-45-678901 변수민 으로 보내주세요")
    assert p and p["bank"] == "국민" and p["account_no"] == "123-45-678901" and p["holder"] == "변수민"
    assert refunds.masked_text("국민 123-45-678901 변수민", p) == "국민 ····8901 변수민"
    assert refunds.parse_account("시연은행 000-0000-0001")["account_no"] == "000-0000-0001"
    assert refunds.parse_account("제 번호 010-1234-5678 로 연락 주세요") is None  # 은행 이름 없는 숫자는 계좌로 보지 않음
    assert refunds.parse_account("국민 12-34") is None


def test_workbench_order_first_team_and_actions(db, client, demo):
    ev, base = demo
    up(client, f"{base}/demo/slack", slack_csv())
    wb = client.get(f"{base}/workbench").json()
    order = [t["status"] for t in wb["teams"]]
    assert order == sorted(order, key=["check", "extra", "refund", "done"].index)  # 급한 순
    names = {t["team_id"]: t["name"] for t in wb["teams"]}
    assert wb["urgent"]["action"] == "approve_change" and names[wb["first_team_id"]] == "개발팀"  # 브리핑의 가장 급한 것
    dev = next(t for t in wb["teams"] if t["name"] == "개발팀")
    assert dev["todo"] == ["변경 승인 2"]
    # 부서 상세: 할 일 줄에 승인 칸 정보(규칙 없음·슬랙 총무 결정 환불액), 팀원 표(마감 기준 vs 현재)
    e = client.get(f"{base}/report/teams/{wb['first_team_id']}/evidence").json()
    pend = [c for c in e["changes"] if c["status"] == "pending_approval"]
    assert {c["name"]: c["pending_delta"] for c in pend} == {"정마루": 20000, "한바다": -5000}
    assert {m["name"]: (m["snapshot_choice"], m["choice"], m["pending"]) for m in e["members"]}["정마루"] == ("absent", "absent", True)
    gm = next(t["team_id"] for t in wb["teams"] if t["name"] == "경영팀")
    nr = next(c for c in client.get(f"{base}/report/teams/{gm}/evidence").json()["changes"] if c["name"] == "이나래")
    assert nr["rule_missing"] and nr["suggested_refund"] == 10000
    # 승인 → 결과 보고의 다음 할 일에 부서 id
    r = client.post(f"{base}/changes/{pend[0]['change_id']}/approve", json={"note": ""}).json()
    assert any(t.get("team_id") for t in r["agent_report"]["card"]["todo"])


def test_refund_account_slack_candidate_confirm_pay_delete(db, client, demo, events_on):
    ev, base = demo
    _settle(client, base)
    gm = _team(client, base, "경영팀")
    assert gm["status"] == "refund" and gm["balance"] == -10000, gm
    row = next(r for r in client.get(f"{base}/refunds").json()["rows"] if r["team_id"] == gm["team_id"])
    assert row["state"] == "none" and not row["can_pay"]
    # 담당자 김가람이 슬랙 답장으로 계좌 → 후보(총무 확인 전). 전화번호처럼 은행 이름 없는 숫자는 무시
    r = signed(client, msg_event("UTEST00001", "국민 123-45-678901 김가람 으로 보내주세요", clock.now(), "EvAcct"))
    assert r.json()["kind"] == "refund_account" and r.json()["outcome"] == "account_candidate"
    db.rollback()
    m = db.scalar(select(EvidenceMessage).where(EvidenceMessage.external_id == "EvAcct"))
    assert "678901" not in m.text and "····8901" in m.text  # 근거 메시지에는 뒤 4자리만
    row = next(r for r in client.get(f"{base}/refunds").json()["rows"] if r["team_id"] == gm["team_id"])
    assert row["state"] == "candidate" and row["candidate"]["masked"] == "····8901" and "account_no" not in row["candidate"]
    # 총무 확인 → 저장 → 전체 보기(총무만, 감사 로그에는 번호 없음)
    r = client.post(f"{base}/refunds/{gm['team_id']}/account/{row['candidate']['id']}/confirm").json()
    assert "계좌" in r["agent_report"]["card"]["headline"]
    assert client.post(f"{base}/refunds/{gm['team_id']}/account/reveal", json={"purpose": "copy"}).json()["account_no"] == "123-45-678901"
    # 정산이 끝나기 전에는 계좌를 지울 수 없음
    assert client.delete(f"{base}/refunds/{gm['team_id']}/account").status_code == 409
    # 송금 완료 표시 → 정산 확정 + 환불 지급 기록 → 잔액 0 → 완료
    r = client.post(f"{base}/refunds/{gm['team_id']}/paid").json()
    assert r["amount"] == 10000 and "송금 완료" in r["agent_report"]["card"]["headline"]
    t = _team(client, base, "경영팀")
    assert t["status"] == "done" and t["balance"] == 0 and t["refunds_paid"] == 10000
    assert client.post(f"{base}/refunds/{gm['team_id']}/paid").status_code == 409  # 두 번 기록 안 됨
    # 송금 명세 CSV 만 전체 번호(확인 필요), 보고서 CSV·감사 로그에는 없음
    assert client.get(f"{base}/refunds/payouts.csv").status_code == 422
    sheet = client.get(f"{base}/refunds/payouts.csv?confirm=yes").content.decode("utf-8")
    assert sheet.startswith("﻿") and "123-45-678901" in sheet
    for kind in ("teams", "people", ""):
        assert "678901" not in client.get(f"{base}/report.csv?kind={kind}").content.decode("utf-8")
    db.rollback()
    assert not [a for a in db.scalars(select(AuditLog)) if "678901" in str(a.detail)]
    assert client.delete(f"{base}/refunds/{gm['team_id']}/account").json()["deleted"] == 1
    db.rollback()
    assert db.scalar(select(RefundAccount).where(RefundAccount.team_id == gm["team_id"])) is None


def test_refund_link_and_manager_form(db, client, demo):
    ev, base = demo
    _settle(client, base)
    gm = _team(client, base, "경영팀")
    # 미리보기에는 토큰을 만들지 않고, 보낼 때만 개인 링크가 들어감
    pv = client.post(f"{base}/emails/preview", json={"kind": "refund_notice"}).json()
    assert "보낼 때 담당자 전용 링크" in next(t for t in pv["targets"] if t["team"] == "경영팀")["body"]
    team = db.get(Team, gm["team_id"])
    mgr = db.scalar(select(Participant).where(Participant.name == "김가람"))
    token = refunds.issue_link(db, team, mgr)
    db.commit()
    o = client.post("/api/large/refund-link/open", json={"token": token}).json()
    assert o["team"] == "경영팀" and o["due"] == 10000 and o["account"] is None
    bad = client.post("/api/large/refund-link/submit", json={"token": token, "bank": "시연은행", "account_no": "12-34", "holder": "김가람"})
    assert bad.status_code == 422 and bad.json()["error"]["detail"][0]["field"] == "account_no"
    ok = client.post("/api/large/refund-link/submit", json={"token": token, "bank": "시연은행", "account_no": "000-0000-0001", "holder": "김가람"}).json()
    assert ok["account"]["masked"] == "····0001" and ok["account"]["source"] == "link" and "account_no" not in ok["account"]
    assert client.post("/api/large/refund-link/open", json={"token": "nope"}).status_code == 401
    # 7일 지나면 만료
    before = clock.now()
    clock.freeze(before + timedelta(days=8))
    try:
        assert client.post("/api/large/refund-link/open", json={"token": token}).json()["error"]["code"] == "LINK_EXPIRED"
    finally:
        clock.freeze(before)
    # 담당자 화면(역할 전환)에서 다시 입력하면 그 계좌로 바뀜
    assert client.post("/api/large/dev/switch", json={"role": "manager", "participant_id": mgr.id}).status_code == 200
    assert client.get("/api/large/team/refund-account").json()["due"] == 10000
    a = client.post("/api/large/team/refund-account", json={"bank": "시연은행", "account_no": "000-0000-0002", "holder": "김가람"}).json()["account"]
    assert a["masked"] == "····0002" and a["source"] == "manager"
    # 다른 부서 담당자는 이 부서 계좌를 못 넣음(세션의 팀으로만)
    other = db.scalar(select(Participant).where(Participant.name == "최라온"))
    client.post("/api/large/dev/switch", json={"role": "manager", "participant_id": other.id})
    assert client.get("/api/large/team/refund-account").json()["due"] == 0


def test_export_csv_kinds(db, client, demo):
    ev, base = demo
    _settle(client, base)
    teams_csv = client.get(f"{base}/report.csv?kind=teams")
    assert teams_csv.headers["content-type"].startswith("text/csv") and "settlement_teams_v" in teams_csv.headers["content-disposition"]
    text = teams_csv.content.decode("utf-8")
    assert text.startswith("﻿부서,담당자,청구,변동 합계,최종,입금,잔액,상태,근거 요약")
    gm = next(l for l in text.splitlines() if l.startswith("경영팀"))
    assert ",환불," in gm and "이나래" in gm and "박다솜" in gm
    people = client.get(f"{base}/report.csv?kind=people").content.decode("utf-8")
    assert people.startswith("﻿부서,이름,마감 기준,현재,금액,근거,처리 상태")
    nr = next(l for l in people.splitlines() if ",이나래," in l)
    assert ",참석+버스,불참,-10000," in nr and "총무 결정" in nr and "슬랙" in nr
