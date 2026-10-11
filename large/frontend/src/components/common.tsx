import { createContext, useContext, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { api, CHOICE_LABEL, type ChatThread, type Meta } from "../api";

export const MetaContext = createContext<Meta | null>(null);
export const useMeta = () => useContext(MetaContext);

/** 지금 어떤 모드로 도는지 항상 화면 위에 보여준다(모의/실제 연동 구분). */
export function ModeBanner() {
  const meta = useMeta();
  if (!meta) return null;
  const demo = meta.app_mode === "demo";
  return (
    <div className={`banner ${demo ? "" : "prod"}`} role="status">
      {/* 공용 시작 화면은 React 밖(총무봇 hub)이라 일반 링크로 이동 */}
      <a href="/">← 총무봇 시작 화면</a>
      <strong>대모임(LARGE) · {demo ? "시연 모드" : "운영 모드"}</strong>
      {import.meta.env.DEV && <span>· Vite 개발 서버(최종 실행 방식 아님 — 공용 서버는 127.0.0.1:8787)</span>}
      <span>· 이메일: {meta.email_mode === "mock" ? "모의(개발용 수신함에만 기록, 실제 발송 안 함)" : "실제 SMTP 발송"}</span>
      <span>· LLM: {meta.llm_mode === "mock" ? "모의(규칙 기반, API 키 없이 동작)" : "Gemini 실제 연동"}</span>
      {meta.demo_tools && (
        <span>
          · <Link to="/">역할 전환</Link> · <Link to="/dev/inbox">개발용 수신함</Link>
        </span>
      )}
    </div>
  );
}

export function Msg({ m }: { m: { kind: "ok" | "bad"; text: string } | null }) {
  if (!m) return null;
  return <div className={`notice ${m.kind}`}>{m.text}</div>;
}

export function ChoicePill({ choice }: { choice: string | null | undefined }) {
  const key = choice ?? "none";
  const cls = key === "none" || key === "undecided" ? "warn" : key === "absent" ? "" : "ok";
  return <span className={`pill ${cls}`}>{CHOICE_LABEL[key]}</span>;
}

export function StatusPill({ status }: { status: string }) {
  const map: Record<string, [string, string]> = {
    applied: ["반영", "ok"], pending_approval: ["승인 대기", "warn"], rejected: ["반려", "bad"],
    open: ["답변 대기", "warn"], answered: ["답변됨", ""], resolved: ["해결", "ok"], escalated: ["총무 이관", "bad"],
    proposed: ["제안", "warn"], confirmed: ["확정", "ok"], cancelled: ["취소", "bad"], draft: ["초안", ""], superseded: ["이전 버전", ""],
    queued: ["대기", ""], retry_wait: ["재시도 대기", "warn"], sent: ["발송", "ok"], skipped: ["건너뜀", ""], failed: ["실패", "bad"],
  };
  const [label, cls] = map[status] ?? [status, ""];
  return <span className={`pill ${cls}`}>{label}</span>;
}

const STATE_LABEL: Record<string, string> = { idle: "", awaiting_info: "Agent 가 추가 정보를 기다리는 중", awaiting_confirmation: "확인(네/아니요)을 기다리는 중" };

/** Agent 채팅. 대화 상태는 서버에 저장되어 나중에 돌아와도 이어진다. */
export function Chat({ base, title, examples }: { base: string; title: string; examples: string[] }) {
  const [thread, setThread] = useState<ChatThread | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api.get<ChatThread>(base).then(setThread).catch((e) => setError(e.message));
  }, [base]);
  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [thread]);

  async function send(msg: string) {
    if (!msg.trim()) return;
    setBusy(true);
    setError("");
    try {
      const out = await api.post<{ thread: ChatThread }>(base, { text: msg });
      setThread(out.thread);
      setText("");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const submit = (e: FormEvent) => {
    e.preventDefault();
    send(text);
  };

  return (
    <div className="card chat" aria-label={title}>
      <h2>{title}</h2>
      <div className="log" ref={logRef}>
        {thread?.messages.length === 0 && <p className="muted">예: {examples.map((x) => `“${x}”`).join(", ")}</p>}
        {thread?.messages.map((m) => (
          <div key={m.id} className={`bubble ${m.sender === "user" ? "user" : "agent"}`}>
            {m.text}
            {(m.tools.length > 0 || m.rules.length > 0) && (
              <div className="trace">
                {m.tools.map((t, i) => (
                  <span key={i}>
                    {t.dry_run ? "검증" : "도구"}:{t.tool}
                    {t.ok ? "" : "(거절)"}{" "}
                  </span>
                ))}
                {m.rules.length > 0 && <span>· 규칙: {m.rules.join(" / ")}</span>}
              </div>
            )}
          </div>
        ))}
      </div>
      {thread && STATE_LABEL[thread.state] && <div className="notice warn">{STATE_LABEL[thread.state]}</div>}
      {thread?.state === "awaiting_confirmation" && (
        <div className="row">
          <button className="primary" disabled={busy} onClick={() => send("네")}>네</button>
          <button disabled={busy} onClick={() => send("아니요")}>아니요</button>
        </div>
      )}
      <form onSubmit={submit}>
        <input value={text} onChange={(e) => setText(e.target.value)} placeholder="메시지 입력" aria-label="메시지" maxLength={1000} />
        <button className="primary" disabled={busy}>보내기</button>
      </form>
      {error && <p className="error">{error}</p>}
    </div>
  );
}

export function Page({ children, narrow }: { children: ReactNode; narrow?: boolean }) {
  return <main className={`wrap ${narrow ? "narrow" : ""}`}>{children}</main>;
}
