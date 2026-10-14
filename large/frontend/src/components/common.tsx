import { createContext, useContext, useEffect, useRef, useState, type FormEvent, type JSX, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { api, CHOICE_LABEL, STATIC, type ChatMessage, type ChatThread, type Meta } from "../api";

export const MetaContext = createContext<Meta | null>(null);
export const useMeta = () => useContext(MetaContext);

/** 화면 위 공통 상단바: 시작 화면 복귀 · 대모임 브랜드 · 행사명 · 현재 모드(모의/실제) */
export function AppHeader() {
  const meta = useMeta();
  const { name } = useContext(EventNameContext);
  const demo = meta?.app_mode === "demo";
  return (
    <header className="top">
      <div className="top-in">
        {/* 공용 시작 화면은 React 밖(총무봇 공용 서버)이라 일반 링크로 이동. 정적 데모판은 demo/large/ 에 있으므로 한 단계 위 demo/ */}
        <a className="btn ghost small" href={STATIC ? "../" : "/"}>← 시작 화면</a>
        <Link className="brand" to="/"><span className="logo" aria-hidden="true"><img src={`${import.meta.env.BASE_URL}logo.jpg`} alt="" /></span>대모임 총무봇</Link>
        <span className="event" title={name ?? ""}>{name ?? ""}</span>
        {meta && (
          <span className="modes" role="status" aria-label="실행 모드">
            {STATIC && <span className="pill bad" title="미리 녹화한 가상 데이터로 화면만 보여 줍니다. 저장·발송은 공용 서버에서">정적 데모 · 저장 안 됨</span>}
            <span className={`pill ${demo ? "warn" : "ok"}`}>{demo ? "시연 모드" : "운영 모드"}</span>
            <span className={`pill ${meta.notify_mode === "slack" ? "ok" : ""}`} title={meta.notify_mode === "mock" ? "실제로 보내지 않고 개발용 수신함에만 기록" : "소모임과 같은 Slack 봇으로 실제 DM 발송"}>Slack DM {meta.notify_mode === "mock" ? "모의" : "실제"}</span>
            <span className={`pill ${meta.llm_mode !== "mock" ? "ai" : ""}`} title={meta.llm_mode !== "mock" ? "LLM 이 요청 해석·도구 선택. 실패하면 규칙 기반으로 대체(메시지에 표시)" : "API 키 없이 규칙 기반으로 요청 해석"}>
              LLM {meta.llm_mode === "claude" ? "Claude" : meta.llm_mode === "gemini" ? "Gemini" : "모의(규칙)"}
            </span>
            {meta.scheduler && <span className="pill" title="Slack DM 발송 큐 자동 실행">발송 스케줄러 {meta.scheduler.enabled ? `켜짐·${meta.scheduler.interval_seconds}초` : "꺼짐"}</span>}
            {import.meta.env.DEV && <span className="pill bad" title="최종 실행은 공용 서버 127.0.0.1:8787">Vite 개발 서버</span>}
            {meta.demo_tools && !STATIC && <Link className="pill" to="/">역할 전환</Link>}
            {meta.demo_tools && !STATIC && <Link className="pill" to="/dev/inbox">개발용 수신함</Link>}
          </span>
        )}
      </div>
    </header>
  );
}

/** 페이지가 행사 정보를 불러오면 상단바에 행사명을 띄운다. */
export const EventNameContext = createContext<{ name: string | null; setName: (n: string | null) => void }>({ name: null, setName: () => {} });
export function useEventName(name: string | null | undefined) {
  const { setName } = useContext(EventNameContext);
  useEffect(() => {
    if (name) setName(name);
  }, [name, setName]);
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
    queued: ["대기", ""], retry_wait: ["재시도 대기", "warn"], sending: ["발송 중", "info"], sent: ["발송", "ok"], skipped: ["건너뜀", ""], failed: ["실패", "bad"],
  };
  const [label, cls] = map[status] ?? [status, ""];
  return <span className={`pill ${cls}`}>{label}</span>;
}

const STATE_LABEL: Record<string, string> = { idle: "", awaiting_info: "Agent 가 추가 정보를 기다리는 중", awaiting_confirmation: "확인(네/아니요)을 기다리는 중" };

/** Agent 채팅. 대화 상태는 서버에 저장되어 나중에 돌아와도 이어진다. */
export function Chat({ base, title, examples, chips, subtitle, onReply, className }: { base: string; title: string; examples: string[]; chips?: string[]; subtitle?: ReactNode; onReply?: () => void; className?: string }) {
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
      onReply?.();
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
    <div className={`card chat ${className ?? ""}`} aria-label={title}>
      <h2>{title}</h2>
      {subtitle && <div className="muted" style={{ marginTop: -6, marginBottom: 6 }}>{subtitle}</div>}
      <div className="log" ref={logRef}>
        {thread?.messages.length === 0 && <p className="muted">예: {examples.map((x) => `“${x}”`).join(", ")}</p>}
        {thread?.messages.map((m) => (
          <div key={m.id} className={`bubble ${m.sender === "user" ? "user" : "agent"}`}>
            {m.text}
            {m.sender === "agent" && <LlmBadge llm={m.llm} />}
            {(m.tools.length > 0 || m.rules.length > 0) && (
              <div className="trace">
                {m.tools.map((t, i) => (
                  <span key={i}>
                    {t.dry_run ? "코드 검증" : "코드 실행"}:{t.tool}
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
          <button className="yellow" disabled={busy} onClick={() => send("네")}>네</button>
          <button disabled={busy} onClick={() => send("아니요")}>아니요</button>
        </div>
      )}
      {chips && chips.length > 0 && (
        <div className="chips">{chips.map((c) => <button key={c} type="button" className="small" disabled={busy} onClick={() => send(c)}>{c}</button>)}</div>
      )}
      <form onSubmit={submit}>
        <input value={text} onChange={(e) => setText(e.target.value)} placeholder={busy ? "Agent 가 보고서를 보는 중…" : "메시지 입력"} aria-label="메시지" maxLength={1000} />
        <button className="yellow" disabled={busy}>보내기</button>
      </form>
      {error && <p className="error">{error}</p>}
    </div>
  );
}

export function Page({ children, narrow }: { children: ReactNode; narrow?: boolean }) {
  return <main className={`wrap ${narrow ? "narrow" : ""}`}>{children}</main>;
}

/** 이 답변을 누가 정했는지: 실제 LLM / 규칙 기반(모의) / LLM 실패 후 규칙 기반 대체 / 코드의 확인 판정 */
function LlmBadge({ llm }: { llm: ChatMessage["llm"] }) {
  if (!llm) return null;
  const parts: JSX.Element[] = [];
  if (llm.used.includes("gemini")) parts.push(<span key="g" className="pill ai">AI 판단: Gemini{llm.model ? ` (${llm.model})` : ""}</span>);
  if (llm.used.includes("claude")) parts.push(<span key="cl" className="pill ai">AI 판단: Claude{llm.model ? ` (${llm.model})` : ""}</span>);
  if (llm.used.includes("mock")) parts.push(<span key="m" className="pill">규칙 기반 해석(모의)</span>);
  if (llm.used.includes("mock_fallback")) parts.push(<span key="f" className="pill bad" title={llm.fallback}>⚠ LLM 실패 → 규칙 기반 대체</span>);
  if (llm.used.includes("code")) parts.push(<span key="c" className="pill ok">코드가 확인 응답 판정</span>);
  return parts.length ? <div className="trace">{parts}</div> : null;
}
