import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, CHOICE_LABEL, fmtTime, won, type Change, type EventInfo } from "../../api";
import { Msg, Page, StatusPill } from "../../components/common";
import { useAction, useLoad } from "../../hooks";
import { PaymentsTab, RosterTab, SettlementTab, TasksTab } from "./AdminMoney";

export interface Dashboard {
  event: EventInfo;
  modes: { app: string; email: string; llm: string };
  participants: number; teams: number; teams_without_manager: number;
  response_counts: Record<string, number>;
  money: { final_fee: number; received: number; refunds_paid: number; teams_to_collect: number; teams_to_refund: number; teams_blocked: number };
  transactions: { total: number; unallocated: number; proposed_allocations: number };
  open_tasks: Record<string, number>; pending_changes: number; emails: Record<string, number>;
}

const TABS = ["대시보드", "설정", "명단·팀", "변경 승인", "입금 대조", "확인 과제", "정산", "이메일", "Agent 기록"] as const;
type Tab = (typeof TABS)[number];

export default function AdminPage() {
  const nav = useNavigate();
  const { data: events, error } = useLoad(() => api.get<EventInfo[]>("/api/large/admin/events"));
  const [tab, setTab] = useState<Tab>("대시보드");
  if (error) return <Page narrow><div className="card"><p className="error">{error.message}</p><button onClick={() => nav("/admin/login")}>로그인</button></div></Page>;
  if (!events) return <Page><p className="muted">불러오는 중…</p></Page>;
  if (events.length === 0) return <Page narrow><div className="card">관리하는 행사가 없습니다. 시드 데이터를 만들거나 행사를 등록하세요.</div></Page>;
  const ev = events[0];
  const base = `/api/large/admin/events/${ev.id}`;
  return (
    <Page>
      <div className="topbar">
        <h1>총무 · {ev.name}</h1>
        <button onClick={() => api.post("/api/large/auth/admin/logout").then(() => nav("/"))}>로그아웃</button>
      </div>
      <nav className="tabs" aria-label="총무 메뉴">
        {TABS.map((t) => <button key={t} className={t === tab ? "active" : ""} onClick={() => setTab(t)}>{t}</button>)}
      </nav>
      {tab === "대시보드" && <DashboardTab base={base} go={setTab} />}
      {tab === "설정" && <SettingsTab base={base} />}
      {tab === "명단·팀" && <RosterTab base={base} />}
      {tab === "변경 승인" && <ChangesTab base={base} />}
      {tab === "입금 대조" && <PaymentsTab base={base} />}
      {tab === "확인 과제" && <TasksTab base={base} />}
      {tab === "정산" && <SettlementTab base={base} />}
      {tab === "이메일" && <EmailsTab base={base} />}
      {tab === "Agent 기록" && <RunsTab base={base} />}
    </Page>
  );
}

function DashboardTab({ base, go }: { base: string; go: (t: Tab) => void }) {
  const { data: d } = useLoad(() => api.get<Dashboard>(base), [base]);
  if (!d) return <p className="muted">불러오는 중…</p>;
  const rc = d.response_counts;
  return (
    <>
      <div className="grid" style={{ marginBottom: 14 }}>
        <Stat label="참가자 / 팀" value={`${d.participants}명 / ${d.teams}팀`} />
        {Object.entries(rc).map(([k, v]) => <Stat key={k} label={CHOICE_LABEL[k]} value={`${v}명`} />)}
      </div>
      <div className="grid" style={{ marginBottom: 14 }}>
        <Stat label="최종 회비 합계" value={won(d.money.final_fee)} />
        <Stat label="확정 수납액" value={won(d.money.received)} />
        <Stat label="추가 납부 팀 / 추가 환불 팀" value={`${d.money.teams_to_collect} / ${d.money.teams_to_refund}`} />
        <Stat label="정산 확정 불가 팀" value={`${d.money.teams_blocked}팀`} />
      </div>
      <div className="card">
        <h2>지금 사람이 볼 것 (HITL)</h2>
        <ul>
          <li>마감 후 변경 승인 대기 <b>{d.pending_changes}건</b> <button className="small" onClick={() => go("변경 승인")}>보기</button></li>
          <li>Agent 의 입금 배정 제안 <b>{d.transactions.proposed_allocations}건</b>, 미배정 거래 {d.transactions.unallocated}건 <button className="small" onClick={() => go("입금 대조")}>보기</button></li>
          <li>열린 확인 과제: 총무 {d.open_tasks.admin ?? 0} · 담당자 {d.open_tasks.manager ?? 0} <button className="small" onClick={() => go("확인 과제")}>보기</button></li>
          <li>담당자 미지정 팀 {d.teams_without_manager}팀 <button className="small" onClick={() => go("명단·팀")}>보기</button></li>
          <li>이메일 작업: {Object.entries(d.emails).map(([k, v]) => `${k} ${v}`).join(", ") || "없음"} <button className="small" onClick={() => go("이메일")}>보기</button></li>
        </ul>
        <p className="muted">참석 마감 {fmtTime(d.event.attend_deadline)} · 입금 마감 {fmtTime(d.event.payment_deadline)} · 행사 {fmtTime(d.event.event_at)} · 마감 기준 응답 {d.event.snapshot_taken_at ? `저장됨(${fmtTime(d.event.snapshot_taken_at)})` : "아직 없음"}</p>
      </div>
    </>
  );
}

export function Stat({ label, value }: { label: string; value: string }) {
  return <div className="stat"><div className="label">{label}</div><div className="value">{value}</div></div>;
}

interface Rule { id: number; change_type: string; valid_until: string | null; refund_percent: number; description: string }
const toLocalInput = (iso: string | null) => (iso ? iso.slice(0, 16) : "");

function SettingsTab({ base }: { base: string }) {
  const { data: d, reload } = useLoad(() => api.get<Dashboard>(base), [base]);
  const { data: rules, reload: reloadRules } = useLoad(() => api.get<Rule[]>(`${base}/refund-rules`), [base]);
  const { busy, message, run } = useAction();
  const [form, setForm] = useState<Record<string, string>>({});
  const [rule, setRule] = useState({ change_type: "attend_to_absent", valid_until: "", refund_percent: "50", description: "" });
  if (!d) return <p className="muted">불러오는 중…</p>;
  const ev = d.event;
  const val = (k: string, fallback: string) => form[k] ?? fallback;
  const save = () => {
    const body: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(form)) body[k] = k.startsWith("fee_") ? Number(v) : v;
    run(() => api.put(base, body), "저장했습니다.").then(() => { setForm({}); reload(); });
  };
  const locked = !!ev.snapshot_taken_at;
  return (
    <div className="two">
      <div className="card">
        <h2>행사·회비</h2>
        <div style={{ display: "grid", gap: 8 }}>
          <label className="field">행사명<input value={val("name", ev.name)} onChange={(e) => setForm({ ...form, name: e.target.value })} /></label>
          <label className="field">행사 일시<input type="datetime-local" value={val("event_at", toLocalInput(ev.event_at))} onChange={(e) => setForm({ ...form, event_at: e.target.value })} /></label>
          <label className="field">참석 응답 마감<input type="datetime-local" disabled={locked} value={val("attend_deadline", toLocalInput(ev.attend_deadline))} onChange={(e) => setForm({ ...form, attend_deadline: e.target.value })} /></label>
          <label className="field">팀 입금 마감<input type="datetime-local" value={val("payment_deadline", toLocalInput(ev.payment_deadline))} onChange={(e) => setForm({ ...form, payment_deadline: e.target.value })} /></label>
          <div className="row">
            {(["bus", "car", "absent"] as const).map((k) => (
              <label key={k} className="field">{CHOICE_LABEL[k]}<input type="number" min={0} step={1000} disabled={locked} value={val(`fee_${k}`, String(ev.fees[k]))} onChange={(e) => setForm({ ...form, [`fee_${k}`]: e.target.value })} style={{ width: 110 }} /></label>
            ))}
          </div>
          <label className="field">입금 안내<input value={val("account_note", ev.account_note)} onChange={(e) => setForm({ ...form, account_note: e.target.value })} /></label>
          {locked && <p className="muted">마감 기준 응답 저장 후에는 회비와 참석 마감을 바꿀 수 없습니다.</p>}
          <div className="row">
            <button className="primary" disabled={busy || Object.keys(form).length === 0} onClick={save}>저장</button>
            <button disabled={busy || locked || !ev.after_attend_deadline} onClick={() => run(() => api.post(`${base}/snapshot`), "마감 기준 응답을 저장했습니다.").then(reload)}>마감 기준 응답 저장</button>
          </div>
        </div>
        <Msg m={message} />
      </div>
      <div className="card">
        <h2>환불 규칙 (변경 시점·항목별)</h2>
        <p className="muted">감액 변경은 '이전 부담액 − 새 회비'에 환불률을 곱합니다. 맞는 규칙이 없으면 환불 0원(총무 확인). 증액 변경은 차액을 전액 청구합니다.</p>
        <div className="table-wrap"><table>
          <thead><tr><th>항목</th><th>이 시각 전 요청</th><th className="num">환불률</th><th>설명</th><th /></tr></thead>
          <tbody>{rules?.map((r) => (
            <tr key={r.id}><td>{r.change_type === "attend_to_absent" ? "참석→불참" : "버스→자차"}</td><td>{r.valid_until ? fmtTime(r.valid_until) : "기한 없음"}</td><td className="num">{r.refund_percent}%</td><td>{r.description}</td>
              <td><button className="small danger" onClick={() => run(() => api.del(`${base}/refund-rules/${r.id}`), "삭제했습니다.").then(reloadRules)}>삭제</button></td></tr>
          ))}</tbody>
        </table></div>
        <h3>규칙 추가</h3>
        <div className="row">
          <select value={rule.change_type} onChange={(e) => setRule({ ...rule, change_type: e.target.value })} aria-label="변경 항목"><option value="attend_to_absent">참석→불참</option><option value="bus_to_car">버스→자차</option></select>
          <input type="datetime-local" value={rule.valid_until} onChange={(e) => setRule({ ...rule, valid_until: e.target.value })} aria-label="적용 기한" />
          <input type="number" min={0} max={100} value={rule.refund_percent} onChange={(e) => setRule({ ...rule, refund_percent: e.target.value })} style={{ width: 70 }} aria-label="환불률" />%
          <input value={rule.description} onChange={(e) => setRule({ ...rule, description: e.target.value })} placeholder="설명" aria-label="설명" />
          <button disabled={busy} onClick={() => run(() => api.post(`${base}/refund-rules`, { ...rule, valid_until: rule.valid_until || null, refund_percent: Number(rule.refund_percent) }), "추가했습니다.").then(reloadRules)}>추가</button>
        </div>
      </div>
    </div>
  );
}

function ChangesTab({ base }: { base: string }) {
  const [status, setStatus] = useState("pending_approval");
  const { data, reload } = useLoad(() => api.get<Change[]>(`${base}/changes${status ? `?status=${status}` : ""}`), [base, status]);
  const { busy, message, run } = useAction();
  const [manual, setManual] = useState<Record<number, { amount: string; note: string }>>({});
  const decide = (c: Change, ok: boolean) => {
    const m = manual[c.id];
    const body = ok && c.rule_missing ? { note: m?.note ?? "", refund_amount: m?.amount === undefined || m.amount === "" ? null : Number(m.amount) } : { note: "" };
    return run(() => api.post(`${base}/changes/${c.id}/${ok ? "approve" : "reject"}`, body), ok ? "승인했습니다." : "반려했습니다.").then(reload);
  };
  return (
    <div className="card">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2>응답 변경</h2>
        <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="상태"><option value="pending_approval">승인 대기</option><option value="">전체</option><option value="applied">반영</option><option value="rejected">반려</option></select>
      </div>
      <p className="muted">마감 후 변경은 여기서 승인해야 반영됩니다. 금액은 요청 시각의 환불 규칙으로 코드가 계산한 값입니다. 맞는 규칙이 없는 건(환불액 미정)은 규칙을 추가하거나 환불액과 사유를 직접 정해야 승인됩니다.</p>
      <Msg m={message} />
      <div className="table-wrap"><table>
        <thead><tr><th>#</th><th>참가자</th><th>변경</th><th>요청</th><th>경로</th><th className="num">부담액</th><th className="num">환불/추가</th><th>규칙</th><th>상태</th><th /></tr></thead>
        <tbody>{data?.map((c) => (
          <tr key={c.id}>
            <td>{c.id}</td><td>{c.participant_name}</td><td>{c.from_label} → {c.to_label}</td><td>{fmtTime(c.requested_at)}</td><td>{c.source}</td>
            <td className="num">{c.after_deadline ? `${won(c.liability_before)} → ${won(c.liability_after)}` : "마감 전"}</td>
            <td className="num">{c.rule_missing && !c.admin_refund_decided ? <span className="pill warn">환불액 미정</span> : c.refund_amount ? `-${won(c.refund_amount)}` : c.extra_charge ? `+${won(c.extra_charge)}` : "-"}</td>
            <td className="muted">{c.rule_text}</td><td><StatusPill status={c.status} /></td>
            <td>{c.status === "pending_approval" && (
              <div style={{ display: "grid", gap: 4 }}>
                {c.rule_missing && (
                  <div className="row">
                    <input type="number" min={0} placeholder="환불액(원)" aria-label="환불액" style={{ width: 100 }} value={manual[c.id]?.amount ?? ""} onChange={(e) => setManual({ ...manual, [c.id]: { note: manual[c.id]?.note ?? "", amount: e.target.value } })} />
                    <input placeholder="사유(필수)" aria-label="사유" value={manual[c.id]?.note ?? ""} onChange={(e) => setManual({ ...manual, [c.id]: { amount: manual[c.id]?.amount ?? "", note: e.target.value } })} />
                  </div>
                )}
                <div className="row"><button className="small primary" disabled={busy} onClick={() => decide(c, true)}>승인</button><button className="small" disabled={busy} onClick={() => decide(c, false)}>반려</button></div>
              </div>
            )}</td>
          </tr>))}
        </tbody>
      </table></div>
      {data?.length === 0 && <p className="muted">해당하는 변경이 없습니다.</p>}
    </div>
  );
}

interface EmailJob { id: number; kind: string; to_email: string; subject: string; status: string; attempts: number; scheduled_at: string; sent_at: string | null; last_error: string; note: string }
const KIND: Record<string, string> = { invite: "초대", attend_reminder: "참석 독촉", payment_reminder: "입금 독촉", confirm_request: "확인 요청", manager_code: "인증코드" };

function EmailsTab({ base }: { base: string }) {
  const [status, setStatus] = useState("");
  const { data, reload } = useLoad(() => api.get<EmailJob[]>(`${base}/emails${status ? `?status=${status}` : ""}`), [base, status]);
  const { busy, message, run } = useAction();
  const enqueue = (kind: string) => run(() => api.post<{ created: number }>(`${base}/emails/enqueue`, { kind }), (r) => `${KIND[kind]} 작업 ${r.created}건 생성(중복 제외)`).then(reload);
  return (
    <div className="card">
      <h2>이메일 발송 작업</h2>
      <p className="muted">발송 직전에 응답·잔액·확인 대기 상태를 다시 검사합니다. 전송 금지 시간(21~8시)·수신자별 하루 한도·중복 방지·실패 재시도(최대 3회)를 적용합니다. 메일에는 금액 명세 대신 웹 확인 링크만 넣습니다.</p>
      <div className="row">
        <button disabled={busy} onClick={() => enqueue("invite")}>초대 메일 생성</button>
        <button disabled={busy} onClick={() => enqueue("attend_reminder")}>참석 독촉(미응답 개인)</button>
        <button disabled={busy} onClick={() => enqueue("payment_reminder")}>입금 독촉(미납 팀 담당자)</button>
        <button className="primary" disabled={busy} onClick={() => run(() => api.post<Record<string, number | string>>(`${base}/emails/dispatch`), (r) => `처리 ${r.processed}건 · ${JSON.stringify(r)}`).then(reload)}>지금 발송 실행</button>
        <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="상태"><option value="">전체</option>{["queued", "retry_wait", "sent", "skipped", "failed"].map((s) => <option key={s} value={s}>{s}</option>)}</select>
      </div>
      <Msg m={message} />
      <div className="table-wrap"><table>
        <thead><tr><th>#</th><th>종류</th><th>수신</th><th>상태</th><th>시도</th><th>예정/발송</th><th>메모</th></tr></thead>
        <tbody>{data?.map((j) => (
          <tr key={j.id}><td>{j.id}</td><td>{KIND[j.kind] ?? j.kind}</td><td>{j.to_email}</td><td><StatusPill status={j.status} /></td><td>{j.attempts}</td>
            <td>{fmtTime(j.sent_at ?? j.scheduled_at)}</td><td className="muted">{j.note || j.last_error}</td></tr>))}
        </tbody>
      </table></div>
    </div>
  );
}

interface Run { id: number; agent: string; trigger: string; evidence_message_id: number | null; steps: Record<string, unknown>[]; rules: string[]; result: Record<string, unknown>; llm_mode: string; created_at: string }

function RunsTab({ base }: { base: string }) {
  const [agent, setAgent] = useState("");
  const { data } = useLoad(() => api.get<Run[]>(`${base}/agent-runs${agent ? `?agent=${agent}` : ""}`), [base, agent]);
  return (
    <div className="card">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2>Agent 실행 기록 (근거·규칙·결과)</h2>
        <select value={agent} onChange={(e) => setAgent(e.target.value)} aria-label="Agent"><option value="">전체</option><option value="communication">소통 Agent</option><option value="settlement">정산 Agent</option></select>
      </div>
      {data?.map((r) => (
        <details key={r.id} style={{ borderBottom: "1px solid var(--line)", padding: "6px 0" }}>
          <summary>#{r.id} <b>{r.agent === "communication" ? "소통" : "정산"}</b> · {r.trigger} · {fmtTime(r.created_at)} · {String(r.result.summary ?? r.result.outcome ?? "")} <span className="muted">({r.llm_mode})</span></summary>
          {r.evidence_message_id && <p className="muted">근거 메시지 #{r.evidence_message_id}</p>}
          {r.rules.length > 0 && <p>규칙: {r.rules.join(" / ")}</p>}
          <pre className="mail">{JSON.stringify(r.steps, null, 1)}</pre>
        </details>
      ))}
    </div>
  );
}
