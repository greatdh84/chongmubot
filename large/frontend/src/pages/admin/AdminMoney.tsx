import { useState } from "react";
import { api, CHOICE_LABEL, fmtTime, won, type SettlementCalc, type Task } from "../../api";
import { ChoicePill, Msg, StatusPill } from "../../components/common";
import { useAction, useLoad } from "../../hooks";

interface TeamRow { id: number; code: string; name: string; headcount: number; manager: { participant_id: number; name: string; slack_id: string; phone: string } | null; counts: Record<string, number>; final_fee: number; received: number; refunds_paid: number; balance: number; balance_label: string; blockers: string[]; settlement_status: string | null }
interface PRow { id: number; employee_no: string; name: string; slack_id: string; phone: string; team: { id: number; code: string; name: string }; is_manager: boolean; choice: string | null; snapshot_choice: string | null; fee: number | null; pending_change_id: number | null; invite: { issued: number; active: number; latest_expires_at: string | null; last_used_at: string | null } }
interface RosterResult { rows: number; inserted: number; updated: number; teams: number; managers_assigned: number; departments: { code: string; name: string; headcount: number; manager: string | null }[]; warnings: string[] }
interface RowError { line: number; error: string }

function Upload({ label, onFile, busy, primary }: { label: string; onFile: (f: File) => void; busy: boolean; primary?: boolean }) {
  return (
    <label className={`btn ${primary ? "primary" : ""}`}>
      {label}
      <input type="file" accept=".csv,text/csv" hidden disabled={busy} onChange={(e) => { const f = e.target.files?.[0]; if (f) onFile(f); e.target.value = ""; }} />
    </label>
  );
}

/** 부서·명단: 전체 명단 CSV 한 파일 → 소속 부서별로 묶이고 팀 담당자가 지정된다. 부서를 누르면 그 부서 명단. */
export function RosterTab({ base }: { base: string }) {
  const { data: teams, reload: reloadTeams } = useLoad(() => api.get<TeamRow[]>(`${base}/teams`), [base]);
  const [teamId, setTeamId] = useState<number | "">("");
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const { data: people, reload } = useLoad(() => (teamId || query ? api.get<PRow[]>(`${base}/participants?${teamId ? `team_id=${teamId}&` : ""}${query ? `q=${encodeURIComponent(query)}` : ""}`) : Promise.resolve([] as PRow[])), [base, teamId, query]);
  const { busy, message, run, setMessage } = useAction();
  const [result, setResult] = useState<RosterResult | null>(null);
  const [rowErrors, setRowErrors] = useState<RowError[]>([]);
  const refresh = () => { reload(); reloadTeams(); };
  const upload = async (f: File) => {
    setRowErrors([]);
    setResult(null);
    try {
      const r = await api.upload<RosterResult>(`${base}/roster`, f);
      setResult(r);
      setMessage({ kind: "ok", text: `명단 ${r.rows}명 → ${r.teams}개 부서로 묶었습니다 (새로 ${r.inserted} · 수정 ${r.updated} · 담당자 지정 ${r.managers_assigned})` });
      refresh();
    } catch (e) {
      const err = e as { message: string; detail?: unknown };
      setMessage({ kind: "bad", text: err.message });
      if (Array.isArray(err.detail)) setRowErrors(err.detail as RowError[]);
    }
  };
  const sel = teams?.find((t) => t.id === teamId);
  return (
    <>
      <div className="card">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2>전체 명단 → 부서별 묶기</h2>
          <Upload label="명단 CSV 업로드" busy={busy} onFile={upload} primary />
        </div>
        <p className="muted">열: <b>ID, 이름, 전화번호, Slack ID, 소속 부서, 팀 담당자</b>. 소속 부서 하나가 팀 하나가 되고, 팀 담당자 칸(그 부서 팀장의 ID 나 이름)으로 담당자가 지정됩니다. 회비는 담당자가 부서 단위로 걷어 내고, 환불도 담당자에게만 연락합니다. 한 행이라도 오류가 있으면 아무것도 반영하지 않습니다.</p>
        <Msg m={message} />
        {rowErrors.length > 0 && (
          <div className="notice bad"><b>반영하지 않은 이유</b><ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>{rowErrors.map((e, i) => <li key={i}>{e.line}행: {e.error}</li>)}</ul></div>
        )}
        {result?.warnings.length ? <div className="notice warn">{result.warnings.map((w, i) => <div key={i}>{w}</div>)}</div> : null}
        <div className="depts">
          {teams?.map((t) => {
            const c = t.counts;
            const answered = t.headcount - c.none - c.undecided;
            return (
              <button key={t.id} className={`dept ${teamId === t.id ? "selected" : ""}`} onClick={() => setTeamId(teamId === t.id ? "" : t.id)}>
                <div className="row" style={{ justifyContent: "space-between" }}><b>{t.name}</b><span className="muted">{t.code}</span></div>
                <div className="muted">담당자 {t.manager ? <b style={{ color: "var(--ink)" }}>{t.manager.name}</b> : <span className="pill bad">미지정</span>}</div>
                <div className="bar" aria-label={`응답 ${answered}/${t.headcount}`}><span style={{ width: `${(answered / Math.max(1, t.headcount)) * 100}%` }} /></div>
                <div className="muted">{t.headcount}명 · 버스 {c.bus} · 자차 {c.car} · 불참 {c.absent}{c.none + c.undecided ? ` · 미확정 ${c.none + c.undecided}` : ""}</div>
                <div className="row" style={{ gap: 4 }}>
                  <span className={`pill ${t.balance > 0 ? "warn" : t.balance < 0 ? "info" : "ok"}`}>{t.balance === 0 ? "정산 0원" : `${t.balance_label} ${won(Math.abs(t.balance))}`}</span>
                  {t.blockers.length > 0 && <span className="pill bad" title={t.blockers.join("\n")}>확인 {t.blockers.length}</span>}
                </div>
              </button>
            );
          })}
        </div>
      </div>
      <div className="card">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2>{sel ? `${sel.name} 명단` : "명단"} {sel?.manager && <span className="muted">· 담당자 {sel.manager.name} ({sel.manager.slack_id}{sel.manager.phone ? ` · ${sel.manager.phone}` : ""})</span>}</h2>
          <div className="row">
            <input value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && setQuery(q)} placeholder="이름 · ID · Slack ID 검색" aria-label="검색" />
            <button onClick={() => setQuery(q)}>검색</button>
            {(teamId || query) && <button onClick={() => { setTeamId(""); setQuery(""); setQ(""); }}>지우기</button>}
          </div>
        </div>
        {!teamId && !query && <p className="muted">위에서 부서를 누르거나 이름으로 검색하세요.</p>}
        {(teamId || query) && (
          <div className="table-wrap"><table>
            <thead><tr><th>ID</th><th>이름</th><th>부서</th><th>Slack ID</th><th>전화번호</th><th>마감 기준</th><th>현재</th><th className="num">회비</th><th>초대 링크</th><th>작업</th></tr></thead>
            <tbody>{people?.map((p) => (
              <tr key={p.id}>
                <td>{p.employee_no}</td><td>{p.name} {p.is_manager && <span className="pill ok">담당자</span>}</td><td>{p.team.name}</td>
                <td className="muted">{p.slack_id}</td><td className="muted">{p.phone || "-"}</td>
                <td><ChoicePill choice={p.snapshot_choice} /></td><td><ChoicePill choice={p.choice} /> {p.pending_change_id && <span className="pill warn">승인 대기</span>}</td>
                <td className="num">{won(p.fee)}</td>
                <td className="muted">유효 {p.invite.active}/{p.invite.issued} · 사용 {fmtTime(p.invite.last_used_at)}</td>
                <td><div className="row">
                  {!p.is_manager && <button className="small" disabled={busy} onClick={() => run(() => api.post(`${base}/teams/${p.team.id}/manager`, { participant_id: p.id }), `${p.name}님을 ${p.team.name} 담당자로 지정했습니다.`).then(refresh)}>담당자 지정</button>}
                  <button className="small" disabled={busy} onClick={() => run(() => api.post<{ revoked: number; email_status: string }>(`${base}/participants/${p.id}/invite/reissue`), (r) => `기존 링크 ${r.revoked}개 폐기, 새 초대 DM ${r.email_status}`).then(reload)}>링크 재발급</button>
                  <SetResponse base={base} p={p} onDone={refresh} />
                </div></td>
              </tr>))}
            </tbody>
          </table></div>
        )}
      </div>
    </>
  );
}

function SetResponse({ base, p, onDone }: { base: string; p: PRow; onDone: () => void }) {
  const { busy, message, run } = useAction();
  return (
    <>
      <select aria-label="총무 직접 입력" disabled={busy} value="" onChange={(e) => e.target.value && run(() => api.post(`${base}/participants/${p.id}/response`, { choice: e.target.value, note: "총무 확인 후 입력(전화·당일 현장)" }), "기록했습니다. 마감 후 변동은 정산 보고서에 반영됩니다.").then(onDone)}>
        <option value="">응답 직접 입력(당일 변동)…</option>
        {["bus", "car", "absent", "undecided"].map((c) => <option key={c} value={c}>{CHOICE_LABEL[c]}</option>)}
      </select>
      {message?.kind === "bad" && <span className="error">{message.text}</span>}
    </>
  );
}

interface Txn { id: number; txn_at: string; depositor: string; amount: number; memo: string; allocated: number; remaining: number; open_task_ids: number[]; allocations: { id: number; team_id: number; team_code: string; participant_id: number | null; participant_name: string | null; amount: number; status: string; source: string; confidence: string; basis: string }[] }

export function PaymentsTab({ base }: { base: string }) {
  const { data, reload } = useLoad(() => api.get<Txn[]>(`${base}/transactions`), [base]);
  const { data: teams } = useLoad(() => api.get<TeamRow[]>(`${base}/teams`), [base]);
  const [filter, setFilter] = useState<"all" | "todo">("todo");
  const { busy, message, run } = useAction();
  const rows = data?.filter((t) => filter === "all" || t.remaining > 0 || t.allocations.some((a) => a.status === "proposed"));
  return (
    <div className="card">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2>입금 대조</h2>
        <div className="row">
          <Upload label="거래내역 CSV 업로드" busy={busy} onFile={(f) => run(() => api.upload<{ inserted: number; duplicates: number }>(`${base}/transactions`, f), (r) => `새 거래 ${r.inserted}건, 이미 있던 거래 ${r.duplicates}건은 건너뜀`).then(reload)} />
          <button className="primary" disabled={busy} onClick={() => run(() => api.post<{ summary: string }>(`${base}/agent/settlement-review`), (r) => `정산 Agent: ${r.summary}`).then(reload)}>정산 Agent 점검 실행</button>
          <button disabled={busy} onClick={() => run(() => api.post<{ confirmed: number }>(`${base}/allocations/confirm-high`), (r) => `신뢰도 high 제안 ${r.confirmed}건 확정`).then(reload)}>high 제안 일괄 확정</button>
          <select value={filter} onChange={(e) => setFilter(e.target.value as "all" | "todo")} aria-label="필터"><option value="todo">처리할 것만</option><option value="all">전체</option></select>
        </div>
      </div>
      <p className="muted">CSV 열: 거래일시,입금자명,금액,메모. 같은 행을 다시 올려도 중복 저장하지 않습니다. Agent 는 '제안'만 하고, 정산에는 총무가 확정한 배정만 들어갑니다. 금액이 같다는 이유만으로는 배정하지 않습니다.</p>
      <Msg m={message} />
      <div className="table-wrap"><table>
        <thead><tr><th>#</th><th>일시</th><th>입금자</th><th>메모</th><th className="num">금액</th><th className="num">남은 금액</th><th>배정</th><th>수동 배정</th></tr></thead>
        <tbody>{rows?.map((t) => (
          <tr key={t.id}>
            <td>{t.id}</td><td>{fmtTime(t.txn_at)}</td><td>{t.depositor}</td><td>{t.memo}</td><td className="num">{won(t.amount)}</td>
            <td className="num">{t.remaining ? won(t.remaining) : "-"} {t.open_task_ids.length > 0 && <span className="pill warn">과제 {t.open_task_ids.map((i) => `#${i}`).join(",")}</span>}</td>
            <td>{t.allocations.map((a) => (
              <div key={a.id} className="row" style={{ marginBottom: 4 }}>
                <StatusPill status={a.status} /> {a.team_code}{a.participant_name ? `·${a.participant_name}` : ""} {won(a.amount)}
                {a.confidence && <span className="muted">[{a.confidence}]</span>}
                <span className="muted" title={a.basis}>{a.basis.slice(0, 40)}</span>
                {a.status === "proposed" && <button className="small primary" disabled={busy} onClick={() => run(() => api.post(`${base}/allocations/${a.id}/confirm`), "확정했습니다.").then(reload)}>확정</button>}
                {a.status !== "cancelled" && <button className="small" disabled={busy} onClick={() => run(() => api.post(`${base}/allocations/${a.id}/cancel`), "취소했습니다.").then(reload)}>취소</button>}
              </div>))}
            </td>
            <td>{t.remaining > 0 && teams && <ManualAllocate base={base} txn={t} teams={teams} onDone={reload} />}</td>
          </tr>))}
        </tbody>
      </table></div>
    </div>
  );
}

function ManualAllocate({ base, txn, teams, onDone }: { base: string; txn: Txn; teams: TeamRow[]; onDone: () => void }) {
  const [team, setTeam] = useState("");
  const [amount, setAmount] = useState(String(txn.remaining));
  const { busy, message, run } = useAction();
  return (
    <div>
      <div className="row">
        <select value={team} onChange={(e) => setTeam(e.target.value)} aria-label="팀"><option value="">팀 선택</option>{teams.map((t) => <option key={t.id} value={t.id}>{t.name} ({t.code})</option>)}</select>
        <input type="number" value={amount} onChange={(e) => setAmount(e.target.value)} style={{ width: 100 }} aria-label="금액" />
        <button className="small" disabled={busy || !team} onClick={() => run(() => api.post(`${base}/transactions/${txn.id}/allocations`, { team_id: Number(team), amount: Number(amount), basis: "총무 수동 배정", confirm: true }), "배정했습니다.").then(onDone)}>배정</button>
      </div>
      {message?.kind === "bad" && <p className="error">{message.text}</p>}
    </div>
  );
}

const TASK_KIND: Record<string, string> = {
  change_approval: "변경 승인", unknown_depositor: "입금자 불명", ambiguous_depositor: "동명이인 입금", unclear_deposit: "불명확한 입금",
  possible_duplicate: "중복 납부 가능성", unresolved_response: "응답 미확정", correction_request: "정정 요청", deposit_unresolved: "입금 미해결(이관)",
  overpayment_unexplained: "초과 수납 원인 불명", multiple_team_reference: "여러 팀 표기",
};

interface SlackSug { team: string; sender: string; sent_at: string; text: string; why: string }
const sugOf = (t: Task) => (t.evidence as { slack_suggestion?: SlackSug }).slack_suggestion ?? null;

export function TasksTab({ base }: { base: string }) {
  const [status, setStatus] = useState("active");
  const [assignee, setAssignee] = useState("");
  const { data, reload } = useLoad(() => api.get<Task[]>(`${base}/tasks?status=${status}${assignee ? `&assignee=${assignee}` : ""}`), [base, status, assignee]);
  const { busy, message, run } = useAction();
  const [notes, setNotes] = useState<Record<number, string>>({});
  return (
    <div className="card">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2>확인 과제</h2>
        <div className="row">
          <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="상태"><option value="active">처리 중</option><option value="">전체</option><option value="resolved">해결</option></select>
          <select value={assignee} onChange={(e) => setAssignee(e.target.value)} aria-label="담당"><option value="">모든 담당</option><option value="admin">총무</option><option value="manager">팀 담당자</option></select>
        </div>
      </div>
      <p className="muted">정산 Agent 가 불일치를 찾으면 과제를 만들고 담당자에게 묻습니다. 답변으로 정할 수 없으면 총무에게 넘깁니다. 변경 승인은 '변경 승인' 탭에서 처리하세요.</p>
      <Msg m={message} />
      {data?.map((t) => (
        <div key={t.id} style={{ borderBottom: "1px solid var(--line)", padding: "8px 0" }}>
          <div className="row"><b>#{t.id}</b><span className="pill">{TASK_KIND[t.kind] ?? t.kind}</span><StatusPill status={t.status} /><span className="muted">담당: {t.assignee === "admin" ? "총무" : "팀 담당자"} · {t.created_by} · {fmtTime(t.created_at)}</span></div>
          <p style={{ margin: "4px 0" }}>{t.question}</p>
          {t.rule && <p className="muted">규칙: {t.rule}</p>}
          {t.answer && <p className="muted">답변: {t.options.find((o) => o.key === t.answer)?.label ?? t.answer} {t.answer_note}</p>}
          {t.resolution && <p className="muted">처리: {t.resolution}</p>}
          {sugOf(t) && !["resolved", "cancelled"].includes(t.status) && (
            <div className="notice">
              <b>정산 Agent 배정 후보(슬랙 근거)</b> → <b>{sugOf(t)!.team}</b><br />
              {sugOf(t)!.sender} {fmtTime(sugOf(t)!.sent_at)}: “{sugOf(t)!.text}” <span className="muted">· {sugOf(t)!.why}</span>
              <div className="row" style={{ marginTop: 6 }}>
                <button className="small primary" disabled={busy} onClick={() => run(() => api.post(`${base}/tasks/${t.id}/accept-suggestion`), `${sugOf(t)!.team} 입금으로 지정·확정했습니다.`).then(reload)}>근거 확인 → {sugOf(t)!.team} 입금으로 지정</button>
                <span className="muted">자동으로 배정하지 않습니다. 총무가 지정하면 확정 배정 + 이 거래의 과제를 닫습니다.</span>
              </div>
            </div>
          )}
          <details><summary className="muted">근거</summary><pre className="mail">{JSON.stringify(t.evidence, null, 1)}</pre></details>
          {t.assignee === "admin" && t.kind !== "change_approval" && !["resolved", "cancelled"].includes(t.status) && (
            <div className="row">
              <input value={notes[t.id] ?? ""} onChange={(e) => setNotes({ ...notes, [t.id]: e.target.value })} placeholder="처리 내용(예: 은행 확인 결과 T07 입금 → 수동 배정)" aria-label="처리 내용" style={{ flex: 1 }} />
              <button className="small primary" disabled={busy || !notes[t.id]} onClick={() => run(() => api.post(`${base}/tasks/${t.id}/resolve`, { resolution: notes[t.id] }), "처리 완료로 기록했습니다.").then(reload)}>처리 완료</button>
            </div>
          )}
        </div>
      ))}
      {data?.length === 0 && <p className="muted">해당하는 과제가 없습니다.</p>}
    </div>
  );
}

export function SettlementTab({ base }: { base: string }) {
  const { data: teams, reload } = useLoad(() => api.get<TeamRow[]>(`${base}/teams`), [base]);
  const [sel, setSel] = useState<number | null>(null);
  const { busy, message, run } = useAction();
  return (
    <>
      <div className="card">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2>팀별 정산</h2>
          <button disabled={busy} onClick={() => run(() => api.post<{ drafts: number; blocked: number }>(`${base}/settlements/compute`), (r) => `정산안 ${r.drafts}팀 저장(확정 불가 ${r.blocked}팀)`).then(reload)}>정산안 다시 계산·저장</button>
        </div>
        <p className="muted">잔액 = 최종 회비 − 확정 수납액 + 이미 지급한 환불액 (양수 추가 납부, 음수 추가 환불). 확인할 것이 남은 팀은 확정할 수 없습니다.</p>
        <Msg m={message} />
        <div className="table-wrap"><table>
          <thead><tr><th>팀</th><th className="num">최종 회비</th><th className="num">확정 수납</th><th className="num">지급 환불</th><th className="num">잔액</th><th>상태</th><th /></tr></thead>
          <tbody>{teams?.map((t) => (
            <tr key={t.id}>
              <td>{t.code} {t.name}</td><td className="num">{won(t.final_fee)}</td><td className="num">{won(t.received)}</td><td className="num">{won(t.refunds_paid)}</td>
              <td className="num">{won(Math.abs(t.balance))} <span className="muted">{t.balance_label}</span></td>
              <td>{t.settlement_status ? <StatusPill status={t.settlement_status} /> : "-"} {t.blockers.length > 0 && <span className="pill warn">확인 {t.blockers.length}</span>}</td>
              <td><button className="small" onClick={() => setSel(t.id)}>명세</button></td>
            </tr>))}
          </tbody>
        </table></div>
      </div>
      {sel && <TeamStatement base={base} teamId={sel} onChange={reload} />}
    </>
  );
}

export function TeamStatement({ base, teamId, onChange }: { base: string; teamId: number; onChange: () => void }) {
  const { data, reload } = useLoad(() => api.get<{ calc: SettlementCalc; latest: { version: number; status: string; manager_ack_at: string | null } | null }>(`${base}/settlements/${teamId}`), [base, teamId]);
  const { busy, message, run } = useAction();
  const [payout, setPayout] = useState("");
  if (!data) return null;
  const c = data.calc;
  const refresh = () => { reload(); onChange(); };
  return (
    <div className="card">
      <h2>{c.team_code} {c.team_name} 정산 명세 {data.latest && <><StatusPill status={data.latest.status} /> <span className="muted">v{data.latest.version}{data.latest.manager_ack_at ? " · 담당자 확인함" : ""}</span></>}</h2>
      <p>최종 회비 {won(c.final_fee)} − 확정 수납 {won(c.received)} + 지급 환불 {won(c.refunds_paid)} = <b>{won(c.balance)}</b> ({c.balance_label})</p>
      {c.blockers.length > 0 && <div className="notice warn">확정 전 해결: {c.blockers.join(" · ")}</div>}
      <div className="row">
        <button className="primary" disabled={busy || c.blockers.length > 0} onClick={() => run(() => api.post(`${base}/settlements/${teamId}/confirm`), "정산을 확정했습니다.").then(refresh)}>{data.latest?.status === "confirmed" ? "다시 계산해 새 버전으로 확정" : "정산 확정"}</button>
        {c.balance < 0 && (
          <>
            <input type="number" value={payout} onChange={(e) => setPayout(e.target.value)} placeholder={String(-c.balance)} style={{ width: 110 }} aria-label="송금액" />
            <button disabled={busy || !payout} onClick={() => run(() => api.post(`${base}/settlements/${teamId}/payouts`, { amount: Number(payout), memo: "환불 송금" }), "송금 결과를 기록했습니다.").then(() => { setPayout(""); refresh(); })}>환불 송금 결과 기록</button>
          </>
        )}
      </div>
      <Msg m={message} />
      <h3>팀원별</h3>
      <div className="table-wrap"><table>
        <thead><tr><th>이름</th><th>마감 기준</th><th>현재</th><th className="num">회비</th><th>마감 후 변경</th></tr></thead>
        <tbody>{c.lines.map((l) => (
          <tr key={l.participant_id}><td>{l.name}</td><td><ChoicePill choice={l.snapshot_choice} /></td><td><ChoicePill choice={l.choice} /></td><td className="num">{won(l.fee)}</td>
            <td className="muted">{l.changes.map((x) => `${CHOICE_LABEL[x.from ?? "none"]}→${CHOICE_LABEL[x.to]} ${x.status === "applied" ? "" : "(승인 대기)"} ${x.refund_amount ? `환불 ${won(x.refund_amount)}` : x.extra_charge ? `추가 ${won(x.extra_charge)}` : ""} [${x.rule}]`).join(" / ")}</td></tr>))}
        </tbody>
      </table></div>
      <h3>수납</h3>
      <div className="table-wrap"><table>
        <thead><tr><th>일시</th><th>입금자</th><th>구분</th><th className="num">금액</th><th>상태</th><th>근거</th></tr></thead>
        <tbody>{c.receipts.map((r) => <tr key={r.allocation_id}><td>{fmtTime(r.txn_at)}</td><td>{r.depositor}</td><td>{r.kind}</td><td className="num">{won(r.amount)}</td><td><StatusPill status={r.status} /></td><td className="muted">{r.basis}</td></tr>)}</tbody>
      </table></div>
      <p className="muted">적용 규칙: {c.rules.join(" · ")}</p>
    </div>
  );
}
