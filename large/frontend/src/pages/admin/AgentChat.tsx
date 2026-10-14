import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, fmtTime, won, type ChatMessage, type ChatThread } from "../../api";
import { useAction } from "../../hooks";

/** 총무 Agent 보고 카드(백엔드 agents/admin_cards.py 와 같은 구조). 숫자는 모두 코드 계산 값 */
export interface Todo { label: string; action: string; target_id?: number; team?: string; kind?: string }
export interface AgentCard {
  type: string; greeting?: string; headline?: string; status?: { key: string; label: string } | null;
  story?: { at: string | null; text: string; who?: string; quote?: string }[];
  money?: { billed?: number | null; final?: number; diff?: number | null; received?: number | null; balance?: number | null; show_received?: boolean; total?: number; if_approved?: number } | null;
  rows?: { team?: string; name?: string; manager?: string | null; amount?: number; reason?: string; line?: string; kind?: string; team_id?: number; status?: string }[];
  first?: { team_id: number; team: string; reason: string } | null;
  todo?: Todo[]; sources?: { slack?: number; changes?: number; receipts?: number; team_id?: number | null; teams?: number; people?: number };
  tools?: string[]; items?: { icon: string; text: string }[]; urgent?: Todo | null; llm_note?: string; by_llm?: boolean;
}
type Msg = ChatMessage;

/** 총무 행동 뒤 Agent 결과 보고(채팅에도 남음)를 화면 메시지로 */
export type AgentReported = { agent_report?: { card?: { headline?: string } | null } };
export const agentLine = (r: AgentReported, fallback: string) => (r.agent_report?.card?.headline ? `🤖 ${r.agent_report.card.headline}` : fallback);
export type Go = (tab: string, sub?: string, teamId?: number) => void;

const TONE: Record<string, string> = { extra: "warn", refund: "info", check: "bad", done: "ok" };
const signed = (v: number | null | undefined) => (v === null || v === undefined ? "" : v === 0 ? "0원" : `${v > 0 ? "+" : "−"}${Math.abs(v).toLocaleString("ko-KR")}원`);
const EXEC = new Set(["approve_change", "accept_suggestion", "confirm_allocation"]);
const EXEC_WORD: Record<string, string> = { approve_change: "승인", accept_suggestion: "지정", confirm_allocation: "확정" };

/** 카드 한 장: 인사 → 굵은 결론 + 상태 칩 → 무슨 일이 있었나요 → 금액 한 줄 → 총무님이 하실 일 → 근거·도구 */
export function CardView({ c, onTodo, busy }: { c: AgentCard; onTodo: (t: Todo) => void; busy: boolean }) {
  const [ask, setAsk] = useState<Todo | null>(null);
  const m = c.money;
  const todos = c.todo ?? [];
  return (
    <div className={`acard acard-${c.type}`}>
      {c.greeting && <div className="ac-greet">{c.greeting}</div>}
      {c.headline && <div className="ac-head"><b>{c.headline}</b>{c.status && <span className={`pill ${TONE[c.status.key] ?? ""}`}>{c.status.label}</span>}</div>}
      {c.items && c.items.length > 0 && <ul className="ac-items">{c.items.map((x, i) => <li key={i}><span aria-hidden="true">{x.icon}</span> {x.text}</li>)}</ul>}
      {c.first && <div className="ac-first">가장 먼저 볼 것 · <b>{c.first.team}</b></div>}
      {c.rows && c.rows.length > 0 && (
        <table className="ac-rows"><tbody>{c.rows.map((r, i) => (
          <tr key={i} className={c.first && r.team_id === c.first.team_id ? "hl" : ""}>
            <td><b>{r.name ?? r.team}</b>{r.name && r.team ? <span className="muted"> {r.team}</span> : null}{r.manager ? <span className="muted"> · {r.manager}</span> : null}</td>
            <td className="num">{r.amount ? won(r.amount) : ""}</td>
            <td className="muted">{r.reason ?? r.line}</td>
          </tr>))}</tbody></table>
      )}
      {c.story && c.story.length > 0 && (
        <div className="ac-story">
          <div className="ac-label">무슨 일이 있었나요</div>
          {c.story.map((s, i) => (
            <div key={i} className="ac-line">
              {s.at && <span className="ac-at">{fmtTime(s.at)}</span>} {s.who && <span className="ac-who">{s.who}</span>} {s.who && "— "}
              {s.quote ? <><q>{s.quote}</q><div className="muted">{s.text}</div></> : s.text}
            </div>
          ))}
        </div>
      )}
      {m && (m.billed !== undefined && m.billed !== null ? (
        <div className="ac-money">청구 {won(m.billed)} → 최종 <b>{won(m.final)}</b>{m.diff ? <span className="pill warn">{signed(m.diff)}</span> : null}
          {m.if_approved !== undefined && m.if_approved !== m.final && <span className="muted"> · 승인되면 {won(m.if_approved)}</span>}
          {m.show_received ? <span className="muted"> · 입금 {won(m.received)} · 잔액 {signed(m.balance)}</span> : <span className="muted"> · 입금 대조 전</span>}
        </div>
      ) : m.total ? <div className="ac-money">합계 <b>{won(m.total)}</b></div> : null)}
      {todos.length > 0 && (
        <div className="ac-todo">
          <div className="ac-label">총무님이 하실 일</div>
          {ask ? (
            <div className="row"><span>{ask.label} — 정말 {EXEC_WORD[ask.action]}할까요?</span>
              <button className="small yellow" disabled={busy} onClick={() => { onTodo(ask); setAsk(null); }}>{EXEC_WORD[ask.action]}</button>
              <button className="small" onClick={() => setAsk(null)}>취소</button></div>
          ) : (
            <div className="row">{todos.map((t, i) => (
              <button key={i} className={`small ${i === 0 ? "primary" : ""}`} disabled={busy} onClick={() => (EXEC.has(t.action) ? setAsk(t) : onTodo(t))}>{t.label}</button>
            ))}</div>
          )}
        </div>
      )}
      {(c.sources || (c.tools && c.tools.length > 0)) && (
        <div className="ac-foot">
          {c.sources && (c.sources.slack !== undefined) && (
            <button className="linkish" disabled={!c.sources.team_id} onClick={() => c.sources?.team_id && onTodo({ label: "근거", action: "open_evidence", target_id: c.sources.team_id })}>
              근거: 슬랙 {c.sources.slack}건 · 변경 기록 {c.sources.changes}건 · 입금 {c.sources.receipts}건
            </button>
          )}
          {c.tools && c.tools.length > 0 && <span>도구: {c.tools.map((t, i) => <span key={i}>{i > 0 && " → "}<code>{t}</code></span>)}</span>}
          {c.by_llm && <span className="pill ai">문장: AI</span>}
          {c.llm_note && <span className="pill bad" title={c.llm_note}>AI 문장 버림 → 템플릿</span>}
        </div>
      )}
    </div>
  );
}

const STATE_LABEL: Record<string, string> = { awaiting_confirmation: "확인(네/아니요)을 기다리는 중", awaiting_info: "Agent 가 추가 정보를 기다리는 중" };

/** 대시보드 총무 Agent: 열면 먼저 브리핑, 질문하면 보고 카드, 버튼으로 바로 처리(실행은 확인 후) */
export function AgentChat({ base, chips, go, onChange }: { base: string; chips: string[]; go: Go; onChange?: () => void }) {
  const [thread, setThread] = useState<(ChatThread & { messages: Msg[] }) | null>(null);
  const [brief, setBrief] = useState<AgentCard | null>(null);
  const [text, setText] = useState("");
  const { busy, message, run } = useAction();
  const logRef = useRef<HTMLDivElement>(null);
  const chat = `${base}/agent/chat`;
  const load = () => {
    api.get<ChatThread>(chat).then((t) => setThread(t as ChatThread & { messages: Msg[] })).catch(() => undefined);
    api.get<AgentCard>(`${base}/agent/briefing`).then((b) => { setBrief(b); api.post(`${base}/agent/briefing/seen`).catch(() => undefined); }).catch(() => undefined);
  };
  useEffect(load, [base]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { logRef.current?.scrollTo({ top: logRef.current.scrollHeight }); }, [thread]);

  const send = (msg: string) => msg.trim() && run(async () => {
    const out = await api.post<{ thread: ChatThread }>(chat, { text: msg });
    setThread(out.thread as ChatThread & { messages: Msg[] });
    setText("");
    onChange?.();
  });
  const todo = (t: Todo) => {
    const after = () => { load(); onChange?.(); };
    switch (t.action) {
      case "approve_change": return run(() => api.post(`${base}/changes/${t.target_id}/approve`, { note: "" })).then(after);
      case "accept_suggestion": return run(() => api.post(`${base}/tasks/${t.target_id}/accept-suggestion`)).then(after);
      case "confirm_allocation": return run(() => api.post(`${base}/allocations/${t.target_id}/confirm`)).then(after);
      case "notify_preview": {
        const k = t.kind === "extra" ? "추가 납부" : "환불";
        return send(t.team ? `${t.team} 담당자에게 ${k} 안내 보내줘` : `${k} 대상 담당자에게 안내 보내줘`);
      }
      case "open_changes": return go("입금·확인", "변경 승인");
      case "open_task": return go("입금·확인", "확인 과제");
      case "open_payments": return go("입금·확인", "입금 대조");
      case "open_roster": return go("부서·명단");
      case "open_evidence": return go("정산 보고서", undefined, t.target_id);
    }
  };
  const submit = (e: FormEvent) => { e.preventDefault(); send(text); };
  return (
    <div className="card chat agent-chat" aria-label="총무 Agent">
      <h2>총무 Agent</h2>
      <div className="muted" style={{ marginTop: -6, marginBottom: 6 }}>정산 보고서와 근거(변동 기록·입금·슬랙)를 조회해 비서처럼 보고합니다. 숫자는 코드 계산 값만 쓰고, 승인·발송은 총무님이 확인해야 실행됩니다.</div>
      <div className="log" ref={logRef}>
        {brief && <div className="bubble agent brief"><CardView c={brief} onTodo={todo} busy={busy} /></div>}
        {thread?.messages.map((m) => (
          <div key={m.id} className={`bubble ${m.sender === "user" ? "user" : "agent"}`}>
            {m.sender === "agent" && m.card ? <CardView c={m.card as AgentCard} onTodo={todo} busy={busy} /> : m.text}
            {m.sender === "agent" && !m.card && m.tools.length > 0 && <div className="trace">도구: {m.tools.map((t) => t.tool).join(" → ")}</div>}
          </div>
        ))}
      </div>
      {message?.kind === "bad" && <div className="notice bad">{message.text}</div>}
      {thread && STATE_LABEL[thread.state] && <div className="notice warn">{STATE_LABEL[thread.state]}</div>}
      {thread?.state === "awaiting_confirmation" && (
        <div className="row"><button className="yellow" disabled={busy} onClick={() => send("네")}>네, 보내요</button><button disabled={busy} onClick={() => send("아니요")}>아니요</button></div>
      )}
      <div className="chips">{chips.map((c) => <button key={c} type="button" className="small" disabled={busy} onClick={() => send(c)}>{c}</button>)}</div>
      <form onSubmit={submit}>
        <input value={text} onChange={(e) => setText(e.target.value)} placeholder={busy ? "Agent 가 확인하는 중…" : "총무님, 무엇을 볼까요?"} aria-label="메시지" maxLength={1000} />
        <button className="yellow" disabled={busy}>보내기</button>
      </form>
    </div>
  );
}
