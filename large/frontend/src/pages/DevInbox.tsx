import { useState } from "react";
import { api, fmtTime } from "../api";
import { Page } from "../components/common";
import { useLoad } from "../hooks";

interface Mail { id: number; to_email: string; subject: string; body: string; created_at: string }

/** 모의 이메일 모드의 개발용 수신함. 실제로 발송되지 않은 메일이 여기에만 기록된다. */
export default function DevInbox() {
  const [to, setTo] = useState("");
  const [query, setQuery] = useState("");
  const { data, error, reload } = useLoad(() => api.get<Mail[]>(`/api/large/dev/inbox${query ? `?to=${encodeURIComponent(query)}` : ""}`), [query]);
  return (
    <Page>
      <div className="topbar"><h1>개발용 수신함 <span className="pill warn">모의 이메일 · 실제 발송 아님</span></h1></div>
      <div className="card row">
        <input value={to} onChange={(e) => setTo(e.target.value)} placeholder="수신자 이메일로 거르기" aria-label="수신자" />
        <button onClick={() => setQuery(to.trim())}>거르기</button>
        <button onClick={reload}>새로고침</button>
      </div>
      {error && <p className="error">{error.message}</p>}
      {data?.map((m) => (
        <details className="card" key={m.id}>
          <summary><b>{m.subject}</b> <span className="muted">→ {m.to_email} · {fmtTime(m.created_at)}</span></summary>
          <pre className="mail">{linkify(m.body)}</pre>
        </details>
      ))}
      {data?.length === 0 && <p className="muted">메일이 없습니다.</p>}
    </Page>
  );
}

function linkify(text: string) {
  return text.split(/(https?:\/\/\S+)/g).map((part, i) =>
    /^https?:\/\//.test(part) ? <a key={i} href={part.replace(/^https?:\/\/[^/]+/, "")}>{part}</a> : part,
  );
}
