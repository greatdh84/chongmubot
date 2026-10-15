import { useEffect, useState } from "react";
import { api, CHOICE_LABEL, fmtTime, STATIC, won } from "../../api";
import { ChoiceNow, ChoicePill, DeltaChip, deltaRow, deltaTone, Msg } from "../../components/common";
import { useAction, useLoad } from "../../hooks";
import type { Dashboard } from "./AdminPage";
import { AgentChat, type AgentCard, type AgentReported, type Go, type Todo } from "./AgentChat";
import { AgentTimeline, NotifyPanel, SettlementRunCard, TeamEvidenceView, type RefundRow, type TaskBrief, type TeamEvidenceData } from "./Evidence";
import { STATUS, type Report } from "./Report";

/** 대시보드 = 한 화면 작업대: 위 브리핑·최종 현황 띠·업로드/실행/내려받기 → 왼쪽 급한 순 부서 목록 → 오른쪽 부서 상세(근거 + 그 자리에서 처리) · 환불 송금.
 *  모든 처리는 기존 API(승인·확정·지정·과제·안내·송금)를 그대로 쓰고, 처리하면 Agent 가 결과를 한 줄로 보고한다. */
interface WbTeam { team_id: number; code: string; name: string; manager: string | null; status: "extra" | "refund" | "check" | "done"; balance: number; final_fee: number; billed: number | null; received: number; todo: string[]; refund_state: string | null }
interface Wb { teams: WbTeam[]; orphan_tasks: TaskBrief[]; refunds: number; report_version: number; first_team_id: number | null; urgent: Todo | null }
type Flash = { text: string; next?: { label: string; team_id: number } | null };

const AGENT_CHIPS = ["확인 필요한 부서와 이유", "데이터팀 왜 추가 납부야?", "환불 대상 부서 알려줘", "박채원 팀 현황", "재무팀 왜 정산 완료야?", "전체 요약"];
const FILTERS = ["all", "check", "extra", "refund", "done"] as const;
const signed = (v: number | null | undefined) => (v === null || v === undefined ? "-" : v === 0 ? "0원" : `${v > 0 ? "+" : "−"}${Math.abs(v).toLocaleString("ko-KR")}원`);
const BAL_WORD = (v: number) => (v > 0 ? "더 받을 돈" : v < 0 ? "돌려줄 돈" : "정산 끝");

export function Workbench({ base, go }: { base: string; go: Go }) {
  const { data: d, reload: reloadDash } = useLoad(() => api.get<Dashboard>(base), [base]);
  const { data: rep, reload: reloadRep } = useLoad(() => api.get<Report>(`${base}/report`), [base]);
  const { data: wb, reload: reloadWb } = useLoad(() => api.get<Wb>(`${base}/workbench`), [base]);
  const { data: brief, reload: reloadBrief } = useLoad(() => api.get<AgentCard>(`${base}/agent/briefing`), [base]);
  const { data: refunds, reload: reloadRefunds } = useLoad(() => api.get<{ rows: RefundRow[]; demo_note: string }>(`${base}/refunds`), [base]);
  const [sel, setSel] = useState<number | null>(null);
  const [filter, setFilter] = useState<(typeof FILTERS)[number]>("all");
  const [view, setView] = useState<"team" | "refunds">("team");
  const [flash, setFlash] = useState<Flash | null>(null);
  const [chatOpen, setChatOpen] = useState(false);
  const [tick, setTick] = useState(0);
  const { data: ev, reload: reloadEv } = useLoad(() => (sel ? api.get<TeamEvidenceData>(`${base}/report/teams/${sel}/evidence`) : Promise.resolve(null)), [base, sel]);
  const top = useAction();

  // 첫 진입: 브리핑의 '가장 급한 것' 부서
  useEffect(() => { if (wb && sel === null) setSel(wb.first_team_id); }, [wb, sel]);
  const reloadAll = () => { reloadDash(); reloadRep(); reloadWb(); reloadBrief(); reloadEv(); reloadRefunds(); setTick((n) => n + 1); };
  const openTeam = (id: number) => { setSel(id); setView("team"); setFlash(null); };

  /** 처리 뒤: 숫자 다시 불러오기 + Agent 결과 보고 한 줄 + 다음 할 일 부서 제안 */
  const done = (r: AgentReported | undefined | null, fallback: string) => {
    const card = r?.agent_report?.card as AgentCard | undefined;
    const t = card?.todo?.find((x) => x.team_id && x.team_id !== sel);
    const other = wb?.teams.find((x) => x.team_id !== sel && x.todo.length > 0 && x.status !== "done");
    setFlash({
      text: `🤖 ${card?.headline ?? fallback}`,
      next: t?.team_id ? { label: t.label.replace(/^다음: /, ""), team_id: t.team_id } : other ? { label: `${other.name} — ${other.todo[0]}`, team_id: other.team_id } : null,
    });
    reloadAll();
  };

  if (!d || !rep || !wb) return <p className="muted">작업대를 준비하는 중…</p>;
  const s = rep.summary;
  const rows = wb.teams.filter((t) => filter === "all" || t.status === filter);
  const selTeam = wb.teams.find((t) => t.team_id === sel);
  const txns = d.transactions.total > 0;
  const urgent = brief?.urgent ?? wb.urgent;
  return (
    <div className="bench">
      <div className="card bench-top">
        <div className="bench-brief">
          <span aria-hidden="true">🤖</span>
          <span><span className="muted">Agent 브리핑 · </span><b>{brief?.headline ?? "브리핑을 불러오는 중…"}</b></span>
          {urgent?.team_id && <button className="small primary" onClick={() => openTeam(urgent.team_id!)}>바로 열기</button>}
          <button className="small ghost" onClick={() => setChatOpen(true)}>Agent 에게 묻기</button>
        </div>
        <div className="bench-band" aria-label="최종 현황">
          <span>참석 <b>{s.attending}</b>/{s.headcount}명</span>
          <span>청구 <b>{won(s.billed_total)}</b> → 최종 <b>{won(s.final_total)}</b>{s.billed_final_delta ? <span className="pill warn">{signed(s.billed_final_delta)}</span> : null}</span>
          <span>입금 <b>{txns ? won(s.received_total) : "대조 전"}</b></span>
          <span className="row" style={{ gap: 4 }}>
            {(txns ? (["check", "extra", "refund", "done"] as const) : (["check"] as const)).map((k) => (
              <button key={k} className={`small status-chip tone-${STATUS[k][1]} ${filter === k ? "active" : ""}`} onClick={() => setFilter(filter === k ? "all" : k)}>{STATUS[k][0]} <b>{s.team_status[k] ?? 0}</b></button>
            ))}
            {!txns && <span className="muted">추가·환불·완료는 거래내역을 올려 입금을 맞춘 뒤 나와요</span>}
          </span>
        </div>
        <div className="row bench-actions">
          {!STATIC && <UploadBtn label={txns ? "거래내역 CSV 추가" : "거래내역 CSV 업로드"} primary={!txns} busy={top.busy} onFile={(f) => top.run(() => api.upload<{ inserted: number; duplicates: number }>(`${base}/transactions`, f), (r) => `새 거래 ${r.inserted}건(이미 있던 ${r.duplicates}건 건너뜀). 이제 정산 Agent 를 실행하세요.`).then(reloadAll)} />}
          <button className={txns ? "primary" : ""} disabled={top.busy || !txns} onClick={() => top.run(() => api.post<{ summary: string } & AgentReported>(`${base}/agent/settlement-review`)).then((r) => r && done(r, `정산 Agent: ${r.summary}`))}>정산 Agent 실행</button>
          <button className={d.transactions.proposed_allocations ? "yellow" : ""} disabled={top.busy || !d.transactions.proposed_allocations} onClick={() => top.run(() => api.post<{ confirmed: number } & AgentReported>(`${base}/allocations/confirm-high`)).then((r) => r && done(r, `높은 확신 제안 ${r.confirmed}건을 확정했어요.`))}>높은 확신 제안 일괄 확정</button>
          <span className="spacer" />
          <span className="muted">정산 CSV</span>
          <a className="btn small" href={STATIC ? "report.csv" : `${base}/report.csv?kind=teams`}>부서별 정산표</a>
          <a className="btn small" href={STATIC ? "report.csv" : `${base}/report.csv?kind=people`}>사람별 변동표</a>
        </div>
        <Msg m={top.message} />
      </div>

      <div className="bench-grid">
        <aside className="card bench-list" aria-label="부서 목록">
          <div className="row seg">
            {FILTERS.filter((k) => txns || k === "all" || k === "check").map((k) => (
              <button key={k} className={`small ${filter === k ? "active" : ""}`} onClick={() => setFilter(k)}>{k === "all" ? `전체 ${wb.teams.length}` : `${STATUS[k][0]} ${s.team_status[k] ?? 0}`}</button>
            ))}
          </div>
          <ul className="tlist">
            {rows.map((t) => (
              <li key={t.team_id}>
                <button className={`trow ${sel === t.team_id ? "sel" : ""} st-${txns || t.status === "check" ? t.status : "pre"}`} onClick={() => openTeam(t.team_id)} aria-current={sel === t.team_id}>
                  <span className="t1"><b>{t.name}</b> <span className="muted">{t.manager ?? "담당자 미지정"}</span></span>
                  {txns || t.status === "check" ? <span className={`pill ${STATUS[t.status][1]}`}>{STATUS[t.status][0]}</span> : <span className="pill">대조 전</span>}
                  <span className="t3">{t.todo.length > 0 ? <><span className="todo-badge">할 일 {t.todo.length}</span> <span className="muted">{t.todo.join(" · ")}</span></> : <span className="muted">할 일 없음</span>}</span>
                  <span className="t2" title={txns ? BAL_WORD(t.balance) : "최종 회비(입금 대조 전)"}>{txns ? signed(t.balance) : won(t.final_fee)}</span>
                </button>
              </li>
            ))}
          </ul>
          {rows.length === 0 && <p className="muted">해당하는 부서가 없습니다.</p>}
          {wb.orphan_tasks.length > 0 && <OrphanTasks base={base} tasks={wb.orphan_tasks} onDone={done} />}
        </aside>

        <section className="bench-main">
          <div className="row seg" style={{ marginBottom: 10 }}>
            <button className={view === "team" ? "active" : ""} onClick={() => setView("team")}>부서 상세{selTeam ? ` · ${selTeam.name}` : ""}</button>
            <button className={view === "refunds" ? "active" : ""} onClick={() => setView("refunds")}>환불 송금 {refunds?.rows.length ? `(${refunds.rows.filter((r) => r.state !== "paid").length}/${refunds.rows.length})` : ""}</button>
          </div>
          {flash && (
            <div className="notice ok agent-flash" role="status">
              <span>{flash.text}</span>
              {flash.next && <button className="small primary" onClick={() => openTeam(flash.next!.team_id)}>다음: {flash.next.label} →</button>}
              <button className="small ghost" onClick={() => setFlash(null)} aria-label="닫기">닫기</button>
            </div>
          )}
          {view === "team" ? (
            ev && ev.team.team_id === sel ? <TeamWork key={sel} base={base} e={ev} status={selTeam?.status ?? "check"} txns={txns} onDone={done} go={go} /> : <div className="card"><p className="muted">{sel ? "부서 근거를 모으는 중…" : "왼쪽에서 부서를 고르세요."}</p></div>
          ) : (
            <RefundsPanel base={base} rows={refunds?.rows ?? []} demoNote={refunds?.demo_note ?? ""} refundTeams={s.team_status.refund ?? 0} extraTeams={s.team_status.extra ?? 0} onDone={done} openTeam={openTeam} />
          )}
        </section>
      </div>

      <details className="card bench-more">
        <summary><b>Agent 실행 기록</b> <span className="muted">정산 Agent 입금 대조 카드 · 감지 → 판단 → 실행 → 넘김</span></summary>
        <div className="two" style={{ marginTop: 10 }}>
          <SettlementRunCard key={`s${tick}`} base={base} go={go} />
          <AgentTimeline key={`t${tick}`} base={base} />
        </div>
      </details>

      <div className={`agent-drawer ${chatOpen ? "open" : ""}`}>
        <button className="drawer-toggle yellow" onClick={() => setChatOpen(!chatOpen)} aria-expanded={chatOpen}>🤖 총무 Agent {chatOpen ? "접기 ▾" : "열기 ▴"}</button>
        <div className="drawer-body" hidden={!chatOpen}>
          <AgentChat base={base} chips={AGENT_CHIPS} go={go} reloadKey={tick} openTeam={(id) => { openTeam(id); }} onChange={() => { reloadDash(); reloadRep(); reloadWb(); reloadEv(); reloadRefunds(); }} />
        </div>
      </div>
    </div>
  );
}

function UploadBtn({ label, onFile, busy, primary }: { label: string; onFile: (f: File) => void; busy: boolean; primary?: boolean }) {
  return (
    <label className={`btn ${primary ? "primary" : ""}`}>
      {label}
      <input type="file" accept=".csv,text/csv" hidden disabled={busy} onChange={(e) => { const f = e.target.files?.[0]; if (f) onFile(f); e.target.value = ""; }} />
    </label>
  );
}

type Done = (r: AgentReported | undefined | null, fallback: string) => void;

/** 선택한 부서: 머리(담당자·상태·잔액) → 할 일(그 자리에서 처리) → 근거 ①②③ → 팀원 n명 보기 */
function TeamWork({ base, e, status, txns, onDone, go }: { base: string; e: TeamEvidenceData; status: string; txns: boolean; onDone: Done; go: Go }) {
  const members = e.members ?? [];
  const changed = members.filter((m) => deltaTone(m)).length;
  return (
    <div className="card team-work">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2>{e.team.name} <span className="muted">· 담당자 {e.team.manager ?? "미지정"} · {e.team.headcount}명</span></h2>
        <div className="row">
          {txns || status === "check" ? <span className={`pill ${STATUS[status]?.[1] ?? ""}`}>{STATUS[status]?.[0]}</span> : <span className="pill">입금 대조 전</span>}
          {txns ? <span className="bal">잔액 <b>{signed(e.balance)}</b> <span className="muted">{BAL_WORD(e.balance)}</span></span> : <span className="bal">최종 회비 <b>{won(e.final_fee)}</b></span>}
        </div>
      </div>
      <TodoPanel base={base} e={e} status={txns ? status : status === "check" ? "check" : "pre"} onDone={onDone} />
      <TeamEvidenceView e={e} work go={go} />
      <details className="members">
        <summary><b>팀원 {members.length}명 보기</b> {changed > 0 && <span className="muted">· 마감 기준과 다른 사람 {changed}명</span>} <span className="legend"><span className="pill now-extra">추가</span><span className="pill now-refund">환불·감액</span><span className="pill now-pending">승인 대기</span></span></summary>
        <div className="table-wrap"><table>
          <thead><tr><th>이름</th><th>마감 기준</th><th>현재</th><th className="num">회비</th><th>마감 후 변경</th></tr></thead>
          <tbody>{members.map((m) => {
            const tone = deltaTone(m);
            return (
              <tr key={m.participant_id} className={deltaRow(tone)}>
                <td>{m.name} {m.is_manager && <span className="pill ok">담당자</span>}</td>
                <td><ChoicePill choice={m.snapshot_choice} /></td>
                <td><ChoiceNow choice={m.choice} tone={tone} /></td>
                <td className="num">{won(m.fee)}</td>
                <td>{m.changes.map((c, i) => <span key={i} className="row" style={{ gap: 4 }}>{c.status === "applied" ? <DeltaChip amount={c.amount} text={c.rule_missing ? "환불액 미정" : "0원"} /> : <DeltaChip amount={c.rule_missing ? null : c.amount} pending />} <span className="muted">{c.from}→{c.to}</span></span>)}</td>
              </tr>
            );
          })}</tbody>
        </table></div>
      </details>
    </div>
  );
}

/** 이 부서에서 총무가 할 일 — 버튼을 누르면 그 자리에서 처리(승인·반려·확정·지정·과제 처리·안내·송금) */
function TodoPanel({ base, e, status, onDone }: { base: string; e: TeamEvidenceData; status: string; onDone: Done }) {
  const { busy, message, run } = useAction();
  const [manual, setManual] = useState<Record<number, { amount: string; note: string }>>({});
  const [notes, setNotes] = useState<Record<number, string>>({});
  const act = <T extends AgentReported>(fn: () => Promise<T>, fallback: string) => run(fn).then((r) => r && onDone(r, fallback));
  const pend = e.changes.filter((c) => c.status === "pending_approval");
  const props = e.receipts.filter((r) => r.status === "proposed");
  const tasks = (e.tasks ?? []).filter((t) => t.kind !== "unresolved_response");
  const unresolved = (e.members ?? []).filter((m) => m.unresolved);
  const noticeKind = status === "refund" ? "refund_notice" : status === "extra" ? "extra_notice" : null;
  const notice = noticeKind ? e.notices?.[noticeKind] : undefined;
  const items = pend.length + props.length + tasks.length + unresolved.length + (noticeKind ? 1 : 0) + (e.refund ? 1 : 0);
  return (
    <div className="todo-panel">
      <div className="ac-label">총무님이 할 일 {items > 0 && <span className="hitl" title="사람이 확인해야 반영">HITL</span>}</div>
      <Msg m={message?.kind === "bad" ? message : null} />
      {items === 0 && <p className="muted" style={{ margin: 0 }}>{status === "done" ? "할 일 없음 — 이 부서는 정산이 끝났어요." : status === "pre" ? "지금 할 일은 없어요. 거래내역을 올리면 정산 Agent 가 입금을 맞춰 볼게요." : "지금 총무님이 할 일은 없어요(담당자 답변 대기)."}</p>}
      <ul className="todo">
        {pend.map((c) => {
          const m = manual[c.change_id] ?? { amount: c.suggested_refund != null ? String(c.suggested_refund) : "", note: c.suggested_note ?? "" };
          const set = (v: Partial<typeof m>) => setManual({ ...manual, [c.change_id]: { ...m, ...v } });
          return (
            <li key={`c${c.change_id}`} className="todo-item">
              <div>
                <span className="pill now-pending">변경 승인</span> <b>{c.name}</b> {c.from} → {c.to} {c.on_event_day && <span className="pill warn">행사 당일</span>}{" "}
                {c.rule_missing ? <span className="pill bad">규칙 없음 → 환불액 직접</span> : <DeltaChip amount={c.pending_delta ?? null} pending />}
                <div className="muted">요청 {fmtTime(c.requested_at)} · {c.source}{c.rule_text ? ` · ${c.rule_text}` : ""}</div>
                {c.messages[0] && <div className="quote">슬랙 {c.messages[0].sender} {fmtTime(c.messages[0].sent_at)}: “{c.messages[0].text}”</div>}
                {c.rule_missing && (
                  <div className="row" style={{ marginTop: 4 }}>
                    <input type="number" min={0} placeholder="환불액(원)" aria-label="환불액" style={{ width: 110 }} value={m.amount} onChange={(x) => set({ amount: x.target.value })} />
                    <input placeholder="사유(필수)" aria-label="사유" style={{ flex: 1, minWidth: 160 }} value={m.note} onChange={(x) => set({ note: x.target.value })} />
                    {c.suggested_refund != null && <span className="pill ai" title={c.suggested_note}>슬랙 총무 결정 {won(c.suggested_refund)} 채움</span>}
                  </div>
                )}
              </div>
              <div className="row">
                <button className="small primary" disabled={busy || (c.rule_missing && (m.amount === "" || !m.note.trim()))} onClick={() => act(() => api.post<AgentReported>(`${base}/changes/${c.change_id}/approve`, c.rule_missing ? { note: m.note, refund_amount: Number(m.amount) } : { note: "" }), "승인했어요.")}>승인</button>
                <button className="small" disabled={busy} onClick={() => act(() => api.post<AgentReported>(`${base}/changes/${c.change_id}/reject`, { note: "" }), "반려했어요.")}>반려</button>
              </div>
            </li>
          );
        })}
        {props.map((r) => (
          <li key={`a${r.allocation_id}`} className="todo-item">
            <div>
              <span className="pill warn">입금 제안</span> <b>{r.depositor}</b> {won(r.amount)} <span className="muted">{fmtTime(r.txn_at)} · 확신도 {r.confidence}{r.memo ? ` · 메모 ${r.memo}` : ""}</span>
              <div className="muted">{r.basis}</div>
            </div>
            <div className="row">
              <button className="small primary" disabled={busy} onClick={() => act(() => api.post<AgentReported>(`${base}/allocations/${r.allocation_id}/confirm`), "입금 배정을 확정했어요.")}>확정</button>
              <button className="small" disabled={busy} onClick={() => act(() => api.post<AgentReported>(`${base}/allocations/${r.allocation_id}/cancel`), "배정 제안을 취소했어요.")}>취소</button>
            </div>
          </li>
        ))}
        {tasks.map((t) => (
          <li key={`t${t.id}`} className="todo-item">
            <div>
              <span className="pill bad">{t.label}</span> {t.transaction && <><b>'{t.transaction.depositor}'</b> {won(t.transaction.amount)} <span className="muted">{fmtTime(t.transaction.txn_at)}</span></>}
              <div className="muted">#{t.id} {t.question}{t.assignee === "manager" ? " · 담당자에게 물어봄" : ""}</div>
              {t.slack_suggestion && <div className="quote">슬랙 근거 → <b>{t.slack_suggestion.team}</b> · {t.slack_suggestion.sender} {fmtTime(t.slack_suggestion.sent_at)}: “{t.slack_suggestion.text}” <span className="muted">({t.slack_suggestion.why})</span></div>}
              {!t.slack_suggestion && t.kind !== "possible_duplicate" && (
                <input style={{ marginTop: 4, width: "100%" }} placeholder="처리 내용(예: 은행 확인 결과 T07 입금 → 수동 배정)" aria-label="처리 내용" value={notes[t.id] ?? ""} onChange={(x) => setNotes({ ...notes, [t.id]: x.target.value })} />
              )}
            </div>
            <div className="row">
              {t.slack_suggestion ? (
                <button className="small primary" disabled={busy} onClick={() => act(() => api.post<AgentReported>(`${base}/tasks/${t.id}/accept-suggestion`), `${t.slack_suggestion!.team} 입금으로 지정했어요.`)}>슬랙 근거대로 {t.slack_suggestion.team} 지정</button>
              ) : t.kind === "possible_duplicate" ? (
                <button className="small primary" disabled={busy} onClick={() => act(() => api.post<AgentReported>(`${base}/tasks/${t.id}/resolve`, { resolution: "중복 납부 확인 → 초과분은 환불로 정산" }), "중복 납부로 처리했어요.")}>중복 납부 확인 → 환불로 정산</button>
              ) : (
                <button className="small primary" disabled={busy || !notes[t.id]?.trim()} onClick={() => act(() => api.post<AgentReported>(`${base}/tasks/${t.id}/resolve`, { resolution: notes[t.id] }), "과제를 처리했어요.")}>처리 완료</button>
              )}
            </div>
          </li>
        ))}
        {unresolved.map((m) => (
          <li key={`u${m.participant_id}`} className="todo-item">
            <div><span className="pill bad">응답 미확정</span> <b>{m.name}</b> <span className="muted">마감 기준 {CHOICE_LABEL[m.snapshot_choice ?? "none"]} · 전화·현장 확인 뒤 직접 입력</span></div>
            <select aria-label={`${m.name} 응답 직접 입력`} disabled={busy} value="" onChange={(x) => x.target.value && act(() => api.post<AgentReported>(`${base}/participants/${m.participant_id}/response`, { choice: x.target.value, note: "총무 확인 후 입력(전화·당일 현장)" }), `${m.name}님 응답을 ${CHOICE_LABEL[x.target.value]}(으)로 기록했어요.`)}>
              <option value="">응답 직접 입력…</option>
              {["bus", "car", "absent"].map((k) => <option key={k} value={k}>{CHOICE_LABEL[k]}</option>)}
            </select>
          </li>
        ))}
        {noticeKind && <NoticeItem base={base} teamId={e.team.team_id} kind={noticeKind} manager={e.team.manager} amount={Math.abs(e.balance)} sent={notice} onDone={onDone} />}
        {e.refund && <li className="todo-item"><RefundBox base={base} r={e.refund} onDone={onDone} /></li>}
      </ul>
    </div>
  );
}

interface PvTarget { team_id: number; team: string; manager: string; amount: number; members: { name: string; change: string; amount: number }[]; subject: string; body: string; already_sent: boolean }

/** 이 부서 담당자에게만 안내: 미리보기 → 보내기 */
function NoticeItem({ base, teamId, kind, manager, amount, sent, onDone }: { base: string; teamId: number; kind: "refund_notice" | "extra_notice"; manager: string | null; amount: number; sent?: { status: string; sent_at: string | null; current: boolean }; onDone: Done }) {
  const { busy, message, run } = useAction();
  const [pv, setPv] = useState<PvTarget | null>(null);
  const label = kind === "refund_notice" ? "환불 안내" : "추가 납부 안내";
  const already = sent?.current && sent.status === "sent";
  const open = () => run(() => api.post<{ targets: PvTarget[] }>(`${base}/emails/preview`, { kind })).then((r) => r && setPv(r.targets.find((t) => t.team_id === teamId) ?? null));
  const send = () => run(async () => {
    await api.post(`${base}/emails/enqueue`, { kind, team_ids: [teamId] });
    return api.post<{ sent?: number; deferred?: number; notify_mode: string }>(`${base}/emails/dispatch`);
  }).then((r) => r && (setPv(null), onDone(null, `${manager}님께 ${label}를 보냈어요(${r.notify_mode === "mock" ? "모의 발송 · 개발용 수신함" : "Slack DM"}).${kind === "refund_notice" ? " 계좌가 들어오면 '송금 완료'를 표시하세요." : ""}`)));
  return (
    <li className="todo-item">
      <div>
        <span className={`pill ${kind === "refund_notice" ? "info" : "warn"}`}>{label}</span> 담당자 <b>{manager ?? "미지정"}</b>님께 {won(amount)} {already ? <span className="pill ok">보냄 {fmtTime(sent?.sent_at)}</span> : <span className="muted">· 아직 안 보냄</span>}
        {kind === "refund_notice" && <div className="muted">DM 에 '환불 받을 계좌 입력' 개인 링크(담당자 전용 · 7일)가 들어갑니다.</div>}
        <Msg m={message?.kind === "bad" ? message : null} />
        {pv && (
          <div className="preview-box" style={{ marginTop: 6 }}>
            <b>미리보기 · {pv.team} 담당자 {pv.manager} · {won(pv.amount)}</b>
            <pre className="mail">{pv.subject}{"\n\n"}{pv.body}</pre>
            <div className="row"><button className="yellow small" disabled={busy || pv.already_sent} onClick={send}>보내기</button><button className="small" onClick={() => setPv(null)}>취소</button></div>
          </div>
        )}
      </div>
      {!pv && !STATIC && <button className="small yellow" disabled={busy || !manager} onClick={open}>{already ? "다시 미리보기" : "미리보기"}</button>}
    </li>
  );
}

/** 환불 송금 한 부서: 계좌(뒤 4자리) 상태 → 후보 확인 / 전체 보기·복사 / 송금 완료 표시 / 계좌 정보 삭제. 실제 송금은 하지 않음. */
function RefundBox({ base, r, onDone, compact }: { base: string; r: RefundRow; onDone: Done; compact?: boolean }) {
  const { busy, message, run, setMessage } = useAction();
  const [full, setFull] = useState<string | null>(null);
  const [ask, setAsk] = useState(false);
  const a = r.account;
  const reveal = (purpose: "view" | "copy") => run(() => api.post<{ account_no: string; bank: string; holder: string }>(`${base}/refunds/${r.team_id}/account/reveal`, { purpose })).then(async (x) => {
    if (!x) return;
    if (purpose === "view") setFull(full ? null : `${x.bank} ${x.account_no} ${x.holder}`);
    else { try { await navigator.clipboard.writeText(x.account_no); setMessage({ kind: "ok", text: "계좌번호를 복사했어요(은행 앱에 붙여 넣기)." }); } catch { setFull(`${x.bank} ${x.account_no} ${x.holder}`); } }
  });
  return (
    <div className="refund-box">
      <div>
        {!compact && <><span className="pill info">환불 송금</span> <b>{won(r.refund)}</b> → 담당자 {r.manager} · </>}
        <span className={`pill ${r.state === "paid" ? "ok" : r.state === "saved" ? "info" : r.state === "candidate" ? "ai" : "warn"}`}>{r.state_label}</span>{" "}
        {a && <span><b>{a.bank} {a.masked}</b> · {a.holder} <span className="muted">({a.source_label})</span></span>}
        {!a && r.candidate && <span>슬랙 답장 후보 <b>{r.candidate.bank} {r.candidate.masked}</b> · {r.candidate.holder} <span className="muted">({fmtTime(r.candidate.submitted_at)} · 정산 Agent 가 읽음)</span></span>}
        {!a && !r.candidate && r.state !== "paid" && <span className="muted">{r.notice_sent_at ? `안내 보냄 ${fmtTime(r.notice_sent_at)} · 담당자 입력 대기(링크 만료 ${fmtTime(r.link_expires_at)})` : "환불 안내를 먼저 보내세요(DM 에 계좌 입력 링크)"}</span>}
        {full && <div className="full-acct" aria-live="polite">{full}</div>}
        {r.blockers.length > 0 && r.state !== "paid" && <div className="error" style={{ fontSize: 12 }}>확인 필요 먼저: {r.blockers[0]}</div>}
        <Msg m={message} />
      </div>
      {!STATIC && (
        <div className="row">
          {!a && r.candidate && <button className="small primary" disabled={busy} onClick={() => run(() => api.post<AgentReported>(`${base}/refunds/${r.team_id}/account/${r.candidate!.id}/confirm`)).then((x) => x && onDone(x, "계좌를 저장했어요."))}>확인하고 저장</button>}
          {(a || r.candidate) && r.state !== "paid" && <button className="small ghost" disabled={busy} onClick={() => reveal("view")}>{full ? "가리기" : "전체 보기"}</button>}
          {a && r.state !== "paid" && <button className="small" disabled={busy} onClick={() => reveal("copy")}>계좌 복사</button>}
          {r.can_pay && (ask ? (
            <><span>은행 앱에서 {won(r.due)} 보냈나요?</span><button className="small yellow" disabled={busy} onClick={() => run(() => api.post<AgentReported>(`${base}/refunds/${r.team_id}/paid`)).then((x) => { setAsk(false); if (x) onDone(x, "송금 완료로 표시했어요."); })}>네, 송금 완료</button><button className="small" onClick={() => setAsk(false)}>취소</button></>
          ) : <button className="small yellow" disabled={busy} onClick={() => setAsk(true)}>송금 완료 표시</button>)}
          {r.can_delete && <button className="small danger" disabled={busy} onClick={() => run(() => api.del(`${base}/refunds/${r.team_id}/account`)).then((x) => x && onDone(null, `${r.team} 환불 계좌 정보를 지웠어요.`))}>계좌 정보 삭제</button>}
        </div>
      )}
    </div>
  );
}

/** 환불 송금 목록: 부서·담당자·환불액·계좌(마스킹)·상태 — 기존 '정산 확정·송금 명세'를 여기서 한 번에 */
function RefundsPanel({ base, rows, demoNote, refundTeams, extraTeams, onDone, openTeam }: { base: string; rows: RefundRow[]; demoNote: string; refundTeams: number; extraTeams: number; onDone: Done; openTeam: (id: number) => void }) {
  const total = rows.reduce((n, r) => n + r.refund, 0), paid = rows.reduce((n, r) => n + r.paid, 0);
  const sheet = () => {
    if (!window.confirm("송금 명세 CSV 에는 전체 계좌번호가 들어갑니다. 내려받을까요?")) return;
    window.location.href = `${base}/refunds/payouts.csv?confirm=yes`;
  };
  return (
    <div className="card">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2>환불 송금 <span className="muted">· {rows.length}개 부서 {won(total)} 중 송금 완료 {won(paid)}</span></h2>
        {!STATIC && <button className="small" disabled={!rows.some((r) => r.account)} onClick={sheet}>송금 명세 CSV(전체 계좌번호)</button>}
      </div>
      <div className="notice">💸 <b>실제 송금은 하지 않습니다.</b> 총무님이 은행 앱에서 보낸 뒤 '송금 완료'만 표시하면 정산 확정 + 환불 지급 기록 → 잔액 0 → 완료가 됩니다. 계좌는 뒤 4자리만 보이고, 전체 번호는 [전체 보기]·[계좌 복사]·송금 명세 CSV 에서만(꺼낼 때마다 기록).</div>
      {demoNote && <div className="notice warn">⚠ {demoNote}</div>}
      <NotifyPanel base={base} refundTeams={refundTeams} extraTeams={extraTeams} onSent={() => onDone(null, "담당자 안내를 보냈어요. 환불 DM 에는 계좌 입력 개인 링크가 들어 있어요.")} />
      <div className="table-wrap"><table>
        <thead><tr><th>부서</th><th>담당자</th><th className="num">환불액</th><th>계좌 · 상태 · 처리</th></tr></thead>
        <tbody>{rows.map((r) => (
          <tr key={r.team_id} className={r.state === "paid" ? "" : "delta-refund"}>
            <td><button className="linkish big" onClick={() => openTeam(r.team_id)}>{r.team}</button></td>
            <td>{r.manager ?? "미지정"}</td>
            <td className="num"><b>{won(r.refund)}</b>{r.paid > 0 && r.due > 0 && <div className="muted">남은 {won(r.due)}</div>}</td>
            <td><RefundBox base={base} r={r} onDone={onDone} compact /></td>
          </tr>
        ))}</tbody>
      </table></div>
      {rows.length === 0 && <p className="muted">환불할 부서가 없습니다(거래내역 대조 뒤 생깁니다).</p>}
    </div>
  );
}

/** 부서를 알 수 없는 거래(예: 결산이자) — 총무가 처리 내용만 남긴다 */
function OrphanTasks({ base, tasks, onDone }: { base: string; tasks: TaskBrief[]; onDone: Done }) {
  const { busy, run } = useAction();
  const [note, setNote] = useState<Record<number, string>>({});
  return (
    <div className="orphan">
      <div className="ac-label">부서 밖 과제 {tasks.length}</div>
      {tasks.map((t) => (
        <div key={t.id} className="orphan-item">
          <div><span className="pill bad">{t.label}</span> {t.transaction ? <><b>'{t.transaction.depositor}'</b> {won(t.transaction.amount)}</> : `#${t.id}`}</div>
          <div className="row">
            <input placeholder="처리 내용(예: 은행 결산이자 — 정산 제외)" aria-label="처리 내용" value={note[t.id] ?? ""} onChange={(x) => setNote({ ...note, [t.id]: x.target.value })} style={{ flex: 1, minWidth: 0 }} />
            <button className="small" disabled={busy || !note[t.id]?.trim()} onClick={() => run(() => api.post<AgentReported>(`${base}/tasks/${t.id}/resolve`, { resolution: note[t.id] })).then((r) => r && onDone(r, "과제를 처리했어요."))}>처리</button>
          </div>
        </div>
      ))}
    </div>
  );
}
