import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { Msg, Page, useMeta } from "../components/common";
import { useAction } from "../hooks";

/** 담당자 화면 접근: 명단의 ID(또는 Slack ID)를 넣으면 Slack DM 으로 6자리 인증코드가 온다. */
export default function ManagerLogin() {
  const nav = useNavigate();
  const meta = useMeta();
  const [loginId, setLoginId] = useState("");
  const [code, setCode] = useState("");
  const [sent, setSent] = useState(false);
  const { busy, message, run } = useAction();
  const request = (e: FormEvent) => {
    e.preventDefault();
    run(() => api.post<{ message: string }>("/api/large/auth/manager/request-code", { login_id: loginId }), (r) => r.message).then((r) => r && setSent(true));
  };
  const verify = (e: FormEvent) => {
    e.preventDefault();
    run(async () => {
      await api.post("/api/large/auth/manager/verify", { login_id: loginId, code });
      nav("/manager");
    });
  };
  return (
    <Page narrow>
      <div className="card">
        <h2>팀 담당자 인증 (Slack)</h2>
        <p className="muted">명단 CSV 의 팀 담당자로 지정된 사람만 들어올 수 있습니다. ID 를 넣으면 Slack DM 으로 인증코드가 옵니다.</p>
        <form onSubmit={request} className="row">
          <input required value={loginId} onChange={(e) => setLoginId(e.target.value)} placeholder="명단 ID (예: E260001) 또는 Slack ID" aria-label="ID" />
          <button disabled={busy}>인증코드 받기</button>
        </form>
        {sent && (
          <form onSubmit={verify} className="row" style={{ marginTop: 10 }}>
            <input inputMode="numeric" pattern="\d{6}" required value={code} onChange={(e) => setCode(e.target.value)} placeholder="6자리 코드" aria-label="인증코드" />
            <button className="primary" disabled={busy}>확인</button>
          </form>
        )}
        <Msg m={message} />
        {meta?.notify_mode === "mock" && <p className="muted">모의 발송 모드: Slack DM 대신 <Link to="/dev/inbox">개발용 수신함</Link>에서 코드를 확인하세요.</p>}
      </div>
    </Page>
  );
}
