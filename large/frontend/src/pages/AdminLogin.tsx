import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { Msg, Page } from "../components/common";
import { useAction } from "../hooks";

export default function AdminLogin() {
  const nav = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const { busy, message, run } = useAction();
  const submit = (e: FormEvent) => {
    e.preventDefault();
    run(async () => {
      await api.post("/api/auth/admin/login", { email, password });
      nav("/admin");
    });
  };
  return (
    <Page narrow>
      <form className="card" onSubmit={submit}>
        <h2>총무 로그인</h2>
        <div style={{ display: "grid", gap: 8 }}>
          <label className="field">이메일<input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="username" /></label>
          <label className="field">비밀번호<input type="password" required value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" /></label>
          <button className="primary" disabled={busy}>로그인</button>
        </div>
        <Msg m={message} />
      </form>
    </Page>
  );
}
