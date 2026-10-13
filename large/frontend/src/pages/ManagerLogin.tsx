import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { Msg, Page, useMeta } from "../components/common";
import { useAction } from "../hooks";

/** 담당자 화면 접근: 총무가 지정한 담당자 이메일로 6자리 인증코드를 받아 확인한다. */
export default function ManagerLogin() {
  const nav = useNavigate();
  const meta = useMeta();
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [sent, setSent] = useState(false);
  const { busy, message, run } = useAction();
  const request = (e: FormEvent) => {
    e.preventDefault();
    run(() => api.post<{ message: string }>("/api/large/auth/manager/request-code", { email }), (r) => r.message).then((r) => r && setSent(true));
  };
  const verify = (e: FormEvent) => {
    e.preventDefault();
    run(async () => {
      await api.post("/api/large/auth/manager/verify", { email, code });
      nav("/manager");
    });
  };
  return (
    <Page narrow>
      <div className="card">
        <h2>팀 담당자 이메일 인증</h2>
        <p className="muted">총무가 담당자로 지정한 이메일만 들어올 수 있습니다. 담당자 역할은 직접 고를 수 없습니다.</p>
        <form onSubmit={request} className="row">
          <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} placeholder="회사 이메일" aria-label="이메일" />
          <button disabled={busy}>인증코드 받기</button>
        </form>
        {sent && (
          <form onSubmit={verify} className="row" style={{ marginTop: 10 }}>
            <input inputMode="numeric" pattern="\d{6}" required value={code} onChange={(e) => setCode(e.target.value)} placeholder="6자리 코드" aria-label="인증코드" />
            <button className="primary" disabled={busy}>확인</button>
          </form>
        )}
        <Msg m={message} />
        {meta?.email_mode === "mock" && <p className="muted">모의 이메일 모드: 코드는 <Link to="/dev/inbox">개발용 수신함</Link>에서 확인하세요.</p>}
      </div>
    </Page>
  );
}
