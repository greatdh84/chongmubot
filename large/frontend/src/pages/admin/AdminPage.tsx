import { useEffect, useRef, useState, type JSX } from "react";
import { useNavigate } from "react-router-dom";
import { api, BOT_PERSONA, BOT_STRATEGY, fmtTime, STATIC, won, type Change, type EventInfo } from "../../api";
import { Chat, Msg, Page, StatusPill, useEventName, useMeta } from "../../components/common";
import { useAction, useLoad } from "../../hooks";
import { PaymentsTab, RosterTab, TasksTab } from "./AdminMoney";
import { AgentTimeline, SettlementRunCard } from "./Evidence";
import { Kpi, ReportTab, STATUS, Timeline, triggerLabel, type Report } from "./Report";

export interface Dashboard {
  event: EventInfo;
  modes: { app: string; notify: string; llm: string };
  participants: number; teams: number; teams_without_manager: number;
  response_counts: Record<string, number>;
  money: { final_fee: number; received: number; refunds_paid: number; teams_to_collect: number; teams_to_refund: number; teams_blocked: number };
  transactions: { total: number; unallocated: number; proposed_allocations: number };
  open_tasks: Record<string, number>; pending_changes: number; emails: Record<string, number>;
  stage: string; stage_label: string; report: { version: number; created_at: string; trigger: string } | null;
}

const TABS = ["대시보드", "부서·명단", "입금·확인", "정산 보고서", "기록"] as const;
type Tab = (typeof TABS)[number];

export default function AdminPage() {
  const nav = useNavigate();
  const meta = useMeta();
  const { data: events, error, reload } = useLoad(() => api.get<EventInfo[]>("/api/large/admin/events"));
  const [tab, setTab] = useState<Tab>("대시보드");
  const [checkSub, setCheckSub] = useState<CheckSub>("입금 대조");
  const go = (t: string, sub?: string) => {
    if (sub && (CHECK_SUBS as readonly string[]).includes(sub)) setCheckSub(sub as CheckSub);
    setTab(t as Tab);
    window.scrollTo({ top: 0 });
  };
  useEventName(events?.[0]?.name);
  // 시연 모드: 총무 세션이 없으면 로그인 화면 대신 시연 총무로 바로 들어간다(공용 초기설정 화면에서 넘어온 경우)
  const autoLogin = useRef(false);
  const needLogin = error?.status === 401;
  useEffect(() => {
    if (!needLogin || !meta?.demo_tools || autoLogin.current) return;
    autoLogin.current = true;
    api.post("/api/large/dev/switch", { role: "admin" }).then(reload).catch(() => undefined);
  }, [needLogin, meta, reload]);
  if (needLogin && (meta === null || meta.demo_tools) && !autoLogin.current) return <Page><p className="muted">총무 화면을 여는 중…</p></Page>;
  if (error) return <Page narrow><div className="card"><p className="error">{error.message}</p><button onClick={() => nav("/admin/login")}>로그인</button></div></Page>;
  if (!events) return <Page><p className="muted">불러오는 중…</p></Page>;
  if (events.length === 0) return <Page narrow><div className="card">관리하는 행사가 없습니다. 시드 데이터를 만들거나 행사를 등록하세요.</div></Page>;
  const ev = events[0];
  const base = `/api/large/admin/events/${ev.id}`;
  return (
    <Page>
      <div className="topbar">
        <h1>총무 · {ev.name}</h1>
        <div className="row">
          {ev.bot?.persona && <span className="pill ok" title="공용 초기설정 화면에서 고른 총무봇 말투 · 독촉 전략">총무봇: {BOT_PERSONA[ev.bot.persona] ?? ev.bot.persona}{ev.bot.strategy ? ` · ${BOT_STRATEGY[ev.bot.strategy] ?? ev.bot.strategy}` : ""}</span>}
          {!STATIC && <button onClick={() => api.post("/api/large/auth/admin/logout").then(() => nav(meta?.demo_tools ? "/roles" : "/"))}>로그아웃</button>}
        </div>
      </div>
      <nav className="tabs" aria-label="총무 메뉴">
        {TABS.map((t) => <button key={t} className={t === tab ? "active" : ""} onClick={() => setTab(t)}>{t}</button>)}
      </nav>
      {tab === "대시보드" && <DashboardTab base={base} go={go} />}
      {tab === "부서·명단" && <RosterTab base={base} />}
      {tab === "입금·확인" && <CheckTab base={base} sub={checkSub} setSub={setCheckSub} />}
      {tab === "정산 보고서" && <ReportTab base={base} go={go} />}
      {tab === "기록" && <LogTab base={base} />}
    </Page>
  );
}

const AGENT_CHIPS = ["데이터팀 왜 추가 납부야?", "환불 대상 부서 알려줘", "확인 필요한 부서와 이유", "박채원 팀 현황", "재무팀 왜 정산 완료야?", "전체 요약"];

/** 대시보드: 맨 위 최종 현황 → 왼쪽 총무 Agent(보고서·근거에 묻기) · 오른쪽 정산 Agent 실행 카드·사람이 결정할 것 → Agent 활동 기록 */
function DashboardTab({ base, go }: { base: string; go: (t: string, sub?: string) => void }) {
  const { data: d, reload } = useLoad(() => api.get<Dashboard>(base), [base]);
  const { data: rep, reload: reloadRep } = useLoad(() => api.get<Report>(`${base}/report`), [base]);
  if (!d) return <p className="muted">불러오는 중…</p>;
  const s = rep?.summary;
  const openTasks = Object.values(d.open_tasks).reduce((a, b) => a + b, 0);
  return (
    <>
      {s && (
        <div className="card final-card">
          <div className="row" style={{ justifyContent: "space-between" }}>
            <h2>최종 현황 · {d.stage_label}</h2>
            <button className="small" onClick={() => go("정산 보고서")}>정산 보고서 열기</button>
          </div>
          <div className="grid">
            <Kpi label="참석" value={`${s.attending} / ${s.headcount}명`} sub={s.deadline_counts ? `마감 기준 버스 ${s.deadline_counts.bus} · 자차 ${s.deadline_counts.car} · 불참 ${s.deadline_counts.absent}` : undefined} />
            <Kpi label="청구 → 최종" value={won(s.final_total)} sub={s.billed_total !== null ? `청구 ${won(s.billed_total)} · 차이 ${s.billed_final_delta && s.billed_final_delta > 0 ? "+" : ""}${(s.billed_final_delta ?? 0).toLocaleString("ko-KR")}원` : "납부 마감 전"} />
            <Kpi label="확정 입금" value={won(s.received_total)} sub={`최종 회비의 ${Math.round((s.received_total / Math.max(1, s.final_total)) * 100)}%`} />
            <Kpi label="열린 확인 과제" value={`${openTasks}건`} sub={`총무 ${d.open_tasks.admin ?? 0} · 담당자 ${d.open_tasks.manager ?? 0} · 변경 승인 대기 ${d.pending_changes}`} />
          </div>
          <div className="grid" style={{ marginTop: 10 }}>
            {(["extra", "refund", "check", "done"] as const).map((k) => (
              <button key={k} className={`stat clickable tone-${STATUS[k][1]}`} onClick={() => go("정산 보고서")}>
                <div className="label">{STATUS[k][0]}</div>
                <div className="value">{s.team_status[k]}개 부서</div>
                {k === "extra" && <div className="muted">{won(s.extra_total)}</div>}
                {k === "refund" && <div className="muted">{won(s.refund_total)}</div>}
              </button>
            ))}
          </div>
        </div>
      )}
      <div className="dash">
        <Chat
          base={`${base}/agent/chat`}
          title="총무 Agent 에게 묻기"
          subtitle={<>정산 보고서{rep ? ` v${rep.version}` : ""}와 근거(변동 기록·입금·슬랙 대화)를 조회해 답합니다. 숫자는 코드가 계산한 보고서에서만 가져오고, 담당자 안내 발송은 총무가 '네'라고 해야 실행됩니다.</>}
          examples={AGENT_CHIPS.slice(0, 2)}
          chips={AGENT_CHIPS}
          onReply={() => { reloadRep(); reload(); }}
          className="agent-chat"
        />
        <div>
          <SettlementRunCard base={base} go={go} />
          <div className="card">
            <div className="row" style={{ justifyContent: "space-between" }}>
              <h2>지금 단계 · {d.stage_label}</h2>
            </div>
            {rep && <Timeline rep={rep} />}
            {rep && <p className="muted" style={{ marginBottom: 0 }}>보고서 v{rep.version} · {fmtTime(rep.created_at)} · 계기: {triggerLabel(rep.trigger)}</p>}
          </div>
          <div className="card">
            <h2>사람이 결정할 것 <span className="hitl" title="Human-in-the-loop: 사람이 확인해야 반영">HITL</span></h2>
            <ul className="todo">
              <li><span>마감 후·당일 변경 승인 대기 <b>{d.pending_changes}건</b></span><button className="small" onClick={() => go("입금·확인", "변경 승인")}>보기</button></li>
              <li><span>Agent 입금 배정 제안 <b>{d.transactions.proposed_allocations}건</b> · 미배정 거래 {d.transactions.unallocated}건</span><button className="small" onClick={() => go("입금·확인", "입금 대조")}>보기</button></li>
              <li><span>확인 과제 총무 <b>{d.open_tasks.admin ?? 0}</b> · 담당자 답변 대기 {d.open_tasks.manager ?? 0}</span><button className="small" onClick={() => go("입금·확인", "확인 과제")}>보기</button></li>
              <li><span>팀 담당자 미지정 <b>{d.teams_without_manager}개 부서</b></span><button className="small" onClick={() => go("부서·명단")}>보기</button></li>
            </ul>
          </div>
        </div>
      </div>
      <AgentTimeline base={base} />
    </>
  );
}

const CHECK_SUBS = ["입금 대조", "변경 승인", "확인 과제"] as const;
type CheckSub = (typeof CHECK_SUBS)[number];

/** 입금·확인: 입금 대조 / 변경 승인 / 확인 과제를 한 탭에 */
function CheckTab({ base, sub, setSub }: { base: string; sub: CheckSub; setSub: (s: CheckSub) => void }) {
  return (
    <>
      <div className="row seg" style={{ marginBottom: 12 }}>
        {CHECK_SUBS.map((k) => <button key={k} className={sub === k ? "active" : ""} onClick={() => setSub(k)}>{k}</button>)}
      </div>
      {sub === "입금 대조" && <PaymentsTab base={base} />}
      {sub === "변경 승인" && <ChangesTab base={base} />}
      {sub === "확인 과제" && <TasksTab base={base} />}
    </>
  );
}

/** 기록: Slack 알림 발송 작업과 Agent 실행 기록 */
function LogTab({ base }: { base: string }) {
  const [sub, setSub] = useState<"알림(Slack DM)" | "Agent 기록">("알림(Slack DM)");
  return (
    <>
      <div className="row seg" style={{ marginBottom: 12 }}>
        {(["알림(Slack DM)", "Agent 기록"] as const).map((k) => <button key={k} className={sub === k ? "active" : ""} onClick={() => setSub(k)}>{k}</button>)}
      </div>
      {sub === "알림(Slack DM)" ? <EmailsTab base={base} /> : <RunsTab base={base} />}
    </>
  );
}

export function Stat({ label, value }: { label: string; value: string }) {
  return <div className="stat"><div className="label">{label}</div><div className="value">{value}</div></div>;
}

function ChangesTab({ base }: { base: string }) {
  const [status, setStatus] = useState("pending_approval");
  const { data, reload } = useLoad(() => api.get<Change[]>(`${base}/changes${status ? `?status=${status}` : ""}`), [base, status]);
  const { busy, message, run } = useAction();
  const [manual, setManual] = useState<Record<number, { amount: string; note: string }>>({});
  const decide = (c: Change, ok: boolean) => {
    const m = manual[c.id] ?? (c.suggested_refund != null ? { amount: String(c.suggested_refund), note: c.suggested_note ?? "" } : undefined);
    const body = ok && c.rule_missing ? { note: m?.note ?? "", refund_amount: m?.amount === undefined || m.amount === "" ? null : Number(m.amount) } : { note: "" };
    return run(() => api.post(`${base}/changes/${c.id}/${ok ? "approve" : "reject"}`, body), ok ? "승인했습니다." : "반려했습니다.").then(reload);
  };
  return (
    <div className="card">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2>응답 변경</h2>
        <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="상태"><option value="pending_approval">승인 대기</option><option value="">전체</option><option value="applied">반영</option><option value="rejected">반려</option></select>
      </div>
      <p className="muted">마감 후 변경은 여기서 승인해야 반영됩니다. 정산 Agent 가 슬랙 대화에서 찾은 변경도 '제안'으로만 들어오고(자동 확정 없음), 근거 메시지가 함께 보입니다. 금액은 요청 시각의 환불 규칙으로 코드가 계산한 값입니다. 맞는 규칙이 없는 건(환불액 미정)은 규칙을 추가하거나 환불액과 사유를 직접 정해야 승인됩니다.</p>
      <Msg m={message} />
      <div className="table-wrap"><table>
        <thead><tr><th>#</th><th>참가자</th><th>변경</th><th>요청</th><th>경로</th><th className="num">부담액</th><th className="num">환불/추가</th><th>규칙</th><th>상태</th><th /></tr></thead>
        <tbody>{data?.map((c) => (
          <tr key={c.id}>
            <td>{c.id}</td><td>{c.participant_name}</td>
            <td>{c.from_label} → {c.to_label}{c.evidence?.map((m) => <div key={m.id} className="muted" style={{ maxWidth: 260 }}>슬랙 {m.sender} {fmtTime(m.sent_at)}: “{m.text}”{m.analysis?.confidence ? ` · 확신도 ${m.analysis.confidence}` : ""}</div>)}</td>
            <td>{fmtTime(c.requested_at)}</td><td>{c.source === "slack" ? <span className="pill ai">정산 Agent 제안(슬랙)</span> : c.source}</td>
            <td className="num">{c.after_deadline ? `${won(c.liability_before)} → ${won(c.liability_after)}` : "마감 전"}</td>
            <td className="num">{c.rule_missing && !c.admin_refund_decided ? <span className="pill warn">환불액 미정</span> : c.refund_amount ? `-${won(c.refund_amount)}` : c.extra_charge ? `+${won(c.extra_charge)}` : "-"}</td>
            <td className="muted">{c.rule_text}</td><td><StatusPill status={c.status} /></td>
            <td>{c.status === "pending_approval" && (
              <div style={{ display: "grid", gap: 4 }}>
                {c.rule_missing && (
                  <div className="row">
                    <input type="number" min={0} placeholder="환불액(원)" aria-label="환불액" style={{ width: 100 }} value={manual[c.id]?.amount ?? (c.suggested_refund != null ? String(c.suggested_refund) : "")} onChange={(e) => setManual({ ...manual, [c.id]: { note: manual[c.id]?.note ?? c.suggested_note ?? "", amount: e.target.value } })} />
                    <input placeholder="사유(필수)" aria-label="사유" value={manual[c.id]?.note ?? c.suggested_note ?? ""} onChange={(e) => setManual({ ...manual, [c.id]: { amount: manual[c.id]?.amount ?? (c.suggested_refund != null ? String(c.suggested_refund) : ""), note: e.target.value } })} />
                    {c.suggested_refund != null && <span className="pill warn" title={c.suggested_note}>슬랙 총무 결정 {won(c.suggested_refund)}</span>}
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
const KIND: Record<string, string> = { invite: "인원조사 초대", attend_reminder: "참석 독촉", payment_reminder: "입금 독촉(담당자)", confirm_request: "확인 요청(담당자)", manager_code: "인증코드", refund_notice: "환불 안내(담당자)", extra_notice: "추가 납부 안내(담당자)" };

interface SchedulerStatus { configured: boolean; running: boolean; paused: boolean; enabled: boolean; interval_seconds: number; runs: number; last_run_at: string | null; last_result: Record<string, { processed: number }>; last_error: string; email_mode: string; multi_process: string }

function SchedulerCard({ base, onChange }: { base: string; onChange: () => void }) {
  const { data: s, reload } = useLoad(() => api.get<SchedulerStatus>(`${base}/emails/scheduler`), [base]);
  const { busy, message, run } = useAction();
  if (!s) return null;
  const toggle = (enabled: boolean) => run(() => api.post(`${base}/emails/scheduler`, { enabled }), enabled ? "스케줄러를 다시 켰습니다." : "스케줄러를 일시정지했습니다.").then(() => { reload(); onChange(); });
  return (
    <div className="notice" style={{ display: "grid", gap: 6 }}>
      <div className="row">
        <b>발송 스케줄러</b>
        <span className={`pill ${s.enabled ? "ok" : "warn"}`}>{!s.running ? "꺼짐(설정)" : s.paused ? "일시정지" : `켜짐 · ${s.interval_seconds}초마다`}</span>
        <span className="pill">Slack DM {s.email_mode === "mock" ? "모의(실제 발송 없음)" : "실제 발송"}</span>
        {s.running && (s.paused ? <button className="small primary" disabled={busy} onClick={() => toggle(true)}>재개</button> : <button className="small" disabled={busy} onClick={() => toggle(false)}>일시정지</button>)}
        <button className="small ghost" onClick={reload}>새로고침</button>
      </div>
      <span className="muted">마지막 실행 {fmtTime(s.last_run_at)} · {s.runs}회 · 최근 처리 {Object.values(s.last_result).reduce((n, r) => n + r.processed, 0)}건{s.last_error && ` · 오류: ${s.last_error}`}. 수동 실행과 겹쳐도 같은 DM 은 한 번만 보냅니다(작업 선점). {s.multi_process}.</span>
      {!s.running && <span className="muted">켜려면 서버 환경변수 NOTIFY_SCHEDULER=on 으로 다시 시작하세요.</span>}
      <Msg m={message} />
    </div>
  );
}

function EmailsTab({ base }: { base: string }) {
  const [status, setStatus] = useState("");
  const { data, reload } = useLoad(() => api.get<EmailJob[]>(`${base}/emails${status ? `?status=${status}` : ""}`), [base, status]);
  const { busy, message, run } = useAction();
  const enqueue = (kind: string) => run(() => api.post<{ created: number }>(`${base}/emails/enqueue`, { kind }), (r) => `${KIND[kind]} 작업 ${r.created}건 생성(중복 제외)`).then(reload);
  return (
    <div className="card">
      <h2>알림 발송 작업 (Slack DM)</h2>
      <SchedulerCard base={base} onChange={reload} />
      <p className="muted">인원조사·참석 독촉은 개인에게, 회비(입금 독촉·환불·추가 납부)는 팀 담당자에게만 보냅니다. 발송 직전에 응답·잔액·확인 대기 상태를 다시 검사하고, 전송 금지 시간(21~8시)·수신자별 하루 한도·중복 방지·실패 재시도(최대 3회)를 적용합니다.</p>
      <div className="row">
        <button disabled={busy} onClick={() => enqueue("invite")}>인원조사 초대(개인)</button>
        <button disabled={busy} onClick={() => enqueue("attend_reminder")}>참석 독촉(미응답 개인)</button>
        <button disabled={busy} onClick={() => enqueue("payment_reminder")}>입금 독촉(미납 부서 담당자)</button>
        <button className="primary" disabled={busy} onClick={() => run(() => api.post<Record<string, number | string>>(`${base}/emails/dispatch`), (r) => `처리 ${r.processed}건 · ${JSON.stringify(r)}`).then(reload)}>지금 발송 실행</button>
        <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="상태"><option value="">전체</option>{["queued", "retry_wait", "sending", "sent", "skipped", "failed"].map((s) => <option key={s} value={s}>{s}</option>)}</select>
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

/** 소통 Agent 실행: '누가 무엇을 골랐는지(판단)'와 '코드가 실행한 도구·결과'를 순서대로 */
function RunTimeline({ run }: { run: Run }) {
  const decisions = (run.result.decisions as { by: string; action: Record<string, unknown>; after_steps?: number }[] | undefined) ?? [];
  const steps = run.steps as { tool: string; ok: boolean; dry_run?: boolean; result?: unknown; error?: { code: string; message: string } }[];
  const items: JSX.Element[] = [];
  let si = 0;
  const pushSteps = (until: number) => {
    for (; si < Math.min(until, steps.length); si++) {
      const s = steps[si];
      items.push(<li key={`s${si}`}><span className="pill ok">코드 {s.dry_run ? "검증" : "실행"}</span> {s.tool} → {s.ok ? "성공" : `거절 ${s.error?.code}: ${s.error?.message}`}</li>);
    }
  };
  decisions.forEach((d, i) => {
    pushSteps(d.after_steps ?? si);
    const who = d.by.startsWith("gemini") || d.by.startsWith("claude") ? <span className="pill ai">AI 판단 {d.by.replace("gemini:", "Gemini ").replace("claude:", "Claude ")}</span> : d.by === "code" ? <span className="pill ok">코드 판정</span> : d.by === "mock_fallback" ? <span className="pill bad">LLM 실패 → 규칙 대체</span> : <span className="pill">규칙 기반(모의)</span>;
    items.push(<li key={`d${i}`}>{who} <code>{JSON.stringify(d.action)}</code></li>);
  });
  pushSteps(steps.length);
  const fallback = (run.result.llm as { fallback?: string } | undefined)?.fallback;
  return (
    <>
      {fallback && <div className="notice bad">LLM 실패로 규칙 기반 대체: {fallback}</div>}
      <ol style={{ paddingLeft: 18, fontSize: 13 }}>{items}</ol>
    </>
  );
}

const AGENT: Record<string, string> = { communication: "소통", settlement: "정산", admin: "총무 Agent" };

interface Run { id: number; agent: string; trigger: string; evidence_message_id: number | null; steps: Record<string, unknown>[]; rules: string[]; result: Record<string, unknown>; llm_mode: string; created_at: string }

function RunsTab({ base }: { base: string }) {
  const [agent, setAgent] = useState("");
  const { data } = useLoad(() => api.get<Run[]>(`${base}/agent-runs${agent ? `?agent=${agent}` : ""}`), [base, agent]);
  return (
    <div className="card">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2>Agent 실행 기록 (근거·규칙·결과)</h2>
        <select value={agent} onChange={(e) => setAgent(e.target.value)} aria-label="Agent"><option value="">전체</option><option value="admin">총무 Agent</option><option value="settlement">정산 Agent</option><option value="communication">소통 Agent</option></select>
      </div>
      {data?.map((r) => (
        <details key={r.id} style={{ borderBottom: "1px solid var(--line)", padding: "6px 0" }}>
          <summary>#{r.id} <b>{AGENT[r.agent] ?? r.agent}</b> · {r.trigger} · {fmtTime(r.created_at)} · {String(r.result.summary ?? r.result.outcome ?? "")} <span className="muted">({r.agent === "settlement" ? "규칙 기반 — LLM 미사용" : r.llm_mode})</span></summary>
          {r.evidence_message_id && <p className="muted">근거 메시지 #{r.evidence_message_id}</p>}
          {r.rules.length > 0 && <p>규칙: {r.rules.join(" / ")}</p>}
          {r.agent !== "settlement" ? <RunTimeline run={r} /> : <pre className="mail">{JSON.stringify(r.steps, null, 1)}</pre>}
        </details>
      ))}
    </div>
  );
}
