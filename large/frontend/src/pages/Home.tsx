import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { Msg, Page, useMeta } from "../components/common";
import { useAction } from "../hooks";

interface CastMember { role: "participant" | "manager"; participant_id: number; name: string; team: string; label: string }
interface People { cast: CastMember[]; managers: { participant_id: number; name: string; team: string }[]; participants: { participant_id: number; name: string; team: string; slack_id: string; has_change: boolean }[] }

export default function Home() {
  const meta = useMeta();
  return (
    <Page narrow>
      <div className="topbar"><h1>대모임(LARGE) 총무 Agent: 독촉부터 정산까지</h1></div>
      <div className="card">
        <h2>들어가기</h2>
        <div className="grid">
          <Link className="btn primary" to="/admin/login">총무 로그인</Link>
          <Link className="btn" to="/manager/login">팀 담당자 (Slack 인증)</Link>
        </div>
        <p className="muted">참가자는 Slack DM 으로 받은 <b>개인 초대 링크</b>로 들어옵니다. 링크는 본인 전용이며, 열기만 해서는 아무 응답도 바뀌지 않습니다.</p>
      </div>
      {meta?.demo_tools && <DemoSwitch />}
    </Page>
  );
}

/** 시연 전용 역할 전환. 운영 모드에서는 서버가 /api/large/dev 를 등록하지 않아 쓸 수 없다. */
function DemoSwitch() {
  const nav = useNavigate();
  const [people, setPeople] = useState<People | null>(null);
  const [mgr, setMgr] = useState("");
  const [pid, setPid] = useState("");
  const { busy, message, run } = useAction();
  useEffect(() => {
    api.get<People>("/api/large/dev/people").then((p) => {
      setPeople(p);
      setMgr(String(p.managers[0]?.participant_id ?? ""));
      setPid(String(p.participants[1]?.participant_id ?? ""));
    });
  }, []);
  const go = (role: string, participant_id: number | null, to: string) =>
    run(async () => {
      await api.post("/api/large/dev/switch", { role, participant_id });
      nav(to);
    });
  return (
    <div className="card">
      <h2>시연용 역할 전환 <span className="pill warn">시연 모드 전용</span></h2>
      <p className="muted">실제 흐름(초대 DM 링크, 담당자 Slack 인증)을 건너뛰는 단축 기능입니다. 실제 흐름은 <Link to="/dev/inbox">개발용 수신함</Link>의 DM 링크로 확인하세요.</p>
      <div className="row" style={{ marginBottom: 8 }}>
        <button className="primary" disabled={busy} onClick={() => go("admin", null, "/admin")}>총무로 보기</button>
      </div>
      {people && people.cast.length > 0 && (
        <div style={{ margin: "10px 0" }}>
          <h3>발표 등장인물 (시드 고정)</h3>
          <div style={{ display: "grid", gap: 6 }}>
            {people.cast.map((c) => (
              <button key={`${c.role}-${c.participant_id}`} className="choice" disabled={busy} onClick={() => go(c.role, c.participant_id, c.role === "participant" ? "/me" : "/manager")}>
                <b>{c.team} {c.name}</b> <span className="pill">{c.role === "participant" ? "참가자" : "담당자"}</span>
                <div className="fee">{c.label}</div>
              </button>
            ))}
          </div>
        </div>
      )}
      <div className="row" style={{ marginBottom: 8 }}>
        <select value={mgr} onChange={(e) => setMgr(e.target.value)} aria-label="담당자 선택">
          {people?.managers.map((m) => <option key={m.participant_id} value={m.participant_id}>{m.team} · {m.name}</option>)}
        </select>
        <button disabled={busy || !mgr} onClick={() => go("manager", Number(mgr), "/manager")}>담당자로 보기</button>
      </div>
      <div className="row">
        <select value={pid} onChange={(e) => setPid(e.target.value)} aria-label="참가자 선택">
          {people?.participants.map((p) => <option key={p.participant_id} value={p.participant_id}>{p.team} · {p.name}{p.has_change ? " (변경 있음)" : ""}</option>)}
        </select>
        <button disabled={busy || !pid} onClick={() => go("participant", Number(pid), "/me")}>참가자로 보기</button>
      </div>
      <Msg m={message} />
    </div>
  );
}
