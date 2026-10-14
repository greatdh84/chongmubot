import { useState } from "react";
import { api, CHOICE_LABEL, fmtTime, STATIC, won } from "../../api";
import { Msg, StatusPill } from "../../components/common";
import { useAction, useLoad } from "../../hooks";
import { TeamStatement } from "./AdminMoney";

/** 정산 보고서: 마감 기준표(참석 마감 응답·납부 마감 청구표) + 변동 기록을 정산 Agent 가 합쳐 만든 한 장 */
export interface ReportTeam {
  team_id: number; code: string; name: string; headcount: number;
  manager: { participant_id: number; name: string; slack_id: string } | null;
  counts: Record<string, number>;
  billed: number | null; final_fee: number; received: number; received_proposed: number; paid_by_deadline: number; late_paid: number;
  refunds_paid: number; payment_gap: number | null; change_delta: number | null; balance: number;
  status: "extra" | "refund" | "check" | "done"; blockers: string[]; people_counts: Record<string, number>;
}
export interface ReportPerson {
  participant_id: number; name: string; team_id: number; team_code: string; team_name: string;
  kind: "extra" | "refund" | "pending" | "unresolved" | "rule_missing" | "changed";
  snapshot_choice: string | null; billed_choice: string | null; choice: string | null;
  billed_fee: number | null; final_fee: number | null; amount: number; on_event_day: boolean;
  changes: { change_id: number; from: string; to: string; status: string; requested_at: string; source: string; rule: string; refund_amount: number; extra_charge: number; note: string; on_event_day: boolean }[];
}
export interface ReportSummary {
  stage: string; stage_label: string; checkpoints: { attend_snapshot_at: string | null; payment_billing_at: string | null };
  headcount: number; attending: number; counts: Record<string, number>; teams: number;
  billed_total: number | null; final_total: number; received_total: number; refunds_paid_total: number;
  extra_total: number; refund_total: number; check_total: number;
  team_status: Record<string, number>; people_counts: Record<string, number>; on_event_day_changes: number;
}
export interface Report {
  id: number; version: number; trigger: string; stage: string; created_at: string; summary: ReportSummary; teams: ReportTeam[]; people: ReportPerson[];
  diff: { first?: boolean; teams?: { team_id: number; name: string; before: { balance: number; status: string } | null; after: { balance: number; status: string } }[]; people_added?: { name: string; team: string; kind: string; amount: number }[]; people_removed?: { name: string; team: string; kind: string; amount: number }[] };
  versions?: { version: number; trigger: string; created_at: string; stage: string }[];
  event?: { attend_deadline: string; payment_deadline: string; event_at: string };
}

export const STATUS: Record<string, [string, string]> = { extra: ["추가 납부", "warn"], refund: ["환불", "info"], check: ["확인 필요", "bad"], done: ["정산 완료", "ok"] };
export const KIND: Record<string, [string, string]> = {
  extra: ["추가 납부", "warn"], refund: ["환불", "info"], pending: ["승인 대기", "bad"], unresolved: ["응답 미확정", "bad"], rule_missing: ["환불액 미정", "bad"], changed: ["변경 있음", ""],
};
const TRIGGER: Record<string, string> = {
  seed: "시드", admin_agent: "총무 Agent 질문", report_view: "보고서 열람", checkpoint: "마감 기준표 저장", scheduler_checkpoint: "마감 기준표 저장(스케줄러)",
  change_approved: "변경 승인", change_rejected: "변경 반려", transactions_upload: "거래내역 업로드", allocation_confirmed: "입금 배정 확정",
  allocations_confirmed_high: "입금 배정 일괄 확정", allocation_cancelled: "입금 배정 취소", allocation_manual: "수동 배정", settlement_review: "정산 Agent 점검",
  task_resolved: "확인 과제 처리", settlement_confirmed: "정산 확정", payout_recorded: "환불 송금 기록", admin_response: "총무 직접 입력",
  manager_answer: "담당자 답변", manager_chat: "담당자 채팅", participant_change: "참가자 변경", participant_chat: "참가자 채팅", roster_upload: "명단 업로드", manager_assigned: "담당자 지정",
};
export const triggerLabel = (t: string) => TRIGGER[t] ?? (t.startsWith("manual:") ? "총무가 다시 만들기" : t);
const signed = (v: number | null) => (v === null ? "-" : v === 0 ? "0원" : `${v > 0 ? "+" : "−"}${Math.abs(v).toLocaleString("ko-KR")}원`);

export function ReportTab({ base }: { base: string }) {
  const { data: rep, reload } = useLoad(() => api.get<Report>(`${base}/report`), [base]);
  const [filter, setFilter] = useState<string>("all");
  const [kind, setKind] = useState<string>("all");
  const [sel, setSel] = useState<number | null>(null);
  const { busy, message, run } = useAction();
  if (!rep) return <p className="muted">정산 보고서를 만드는 중…</p>;
  const s = rep.summary;
  const teams = rep.teams.filter((t) => filter === "all" || t.status === filter);
  const people = rep.people.filter((p) => kind === "all" || p.kind === kind);
  const notify = (k: "refund_notice" | "extra_notice") => {
    const label = k === "refund_notice" ? "환불 안내" : "추가 납부 안내";
    const n = k === "refund_notice" ? s.team_status.refund : s.team_status.extra;
    if (!window.confirm(`${label}를 ${n}개 부서 팀 담당자에게 Slack DM 으로 보낼까요?\n(개인에게는 보내지 않습니다. 확인 필요 부서는 제외)`)) return;
    run(async () => {
      const r = await api.post<{ created: number }>(`${base}/emails/enqueue`, { kind: k });
      await api.post(`${base}/emails/dispatch`);
      return r;
    }, (r) => `${label} ${r.created}건 생성·발송(같은 금액으로 이미 보낸 부서는 제외)`);
  };
  return (
    <>
      <div className="card">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div>
            <h2 style={{ marginBottom: 2 }}>정산 보고서 v{rep.version} <span className="pill">{s.stage_label}</span></h2>
            <span className="muted">정산 Agent 가 {fmtTime(rep.created_at)}에 만듦 · 계기: {triggerLabel(rep.trigger)} · 금액은 모두 코드 계산(LLM 미사용)</span>
          </div>
          <div className="row">
            <button disabled={busy} onClick={() => run(() => api.post(`${base}/report/refresh`), "보고서를 다시 만들었습니다(바뀐 내용이 없으면 버전 유지).").then(reload)}>다시 만들기</button>
            {/* 정적 데모판은 녹화 때 함께 저장한 report.csv(demo/large/) */}
            <a className="btn" href={STATIC ? "report.csv" : `${base}/report.csv`} download={STATIC ? `settlement_report_v${rep.version}.csv` : undefined}>CSV 내려받기</a>
          </div>
        </div>
        <Msg m={message} />
        <Timeline rep={rep} />
        <div className="grid" style={{ marginTop: 12 }}>
          <Kpi label="최종 참석" value={`${s.attending}명`} sub={`전체 ${s.headcount}명 · 불참 ${s.counts.absent} · 미확정 ${s.counts.undecided + s.counts.none}`} />
          <Kpi label="납부 마감 청구액" value={won(s.billed_total)} sub={`최종 회비 ${won(s.final_total)}`} />
          <Kpi label="확정 입금" value={won(s.received_total)} sub={s.refunds_paid_total ? `지급 환불 ${won(s.refunds_paid_total)}` : "담당자 팀 일괄 입금 기준"} />
          <Kpi tone="warn" label={`추가 납부 · ${s.team_status.extra}개 부서`} value={won(s.extra_total)} sub={`사람 ${s.people_counts.extra}명`} />
          <Kpi tone="info" label={`환불 · ${s.team_status.refund}개 부서`} value={won(s.refund_total)} sub={`사람 ${s.people_counts.refund}명`} />
          <Kpi tone="bad" label={`확인 필요 · ${s.team_status.check}개 부서`} value={`${s.people_counts.pending + s.people_counts.unresolved + s.people_counts.rule_missing}명`} sub={`당일 변동 ${s.on_event_day_changes}명 · 정산 완료 ${s.team_status.done}개 부서`} />
        </div>
        <p className="muted" style={{ marginTop: 10 }}>부서 정산 잔액 = (청구액 − 확정 입금) + (최종 회비 − 청구액) + 지급 환불. <b>+ 는 담당자가 더 걷어 낼 돈, − 는 총무가 담당자에게 돌려줄 돈</b>입니다. 회비는 팀 단위라 안내는 담당자에게만 갑니다.</p>
        <div className="row">
          <button className="primary" disabled={busy || !s.team_status.refund} onClick={() => notify("refund_notice")}>환불 부서 담당자에게 안내</button>
          <button className="primary" disabled={busy || !s.team_status.extra} onClick={() => notify("extra_notice")}>추가 납부 부서 담당자에게 안내</button>
        </div>
      </div>

      <Diff rep={rep} />

      <div className="card">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2>부서별</h2>
          <div className="row seg" role="tablist" aria-label="상태 필터">
            {["all", "extra", "refund", "check", "done"].map((k) => (
              <button key={k} className={`small ${filter === k ? "active" : ""}`} onClick={() => setFilter(k)}>
                {k === "all" ? `전체 ${rep.teams.length}` : `${STATUS[k][0]} ${s.team_status[k] ?? 0}`}
              </button>
            ))}
          </div>
        </div>
        <div className="table-wrap"><table>
          <thead><tr><th>부서</th><th>담당자</th><th className="num">참석</th><th className="num">청구액</th><th className="num">최종 회비</th><th className="num">확정 입금</th><th>차이 이유</th><th className="num">정산 잔액</th><th>상태</th><th /></tr></thead>
          <tbody>{teams.map((t) => (
            <tr key={t.team_id} className={sel === t.team_id ? "sel" : ""}>
              <td><b>{t.name}</b> <span className="muted">{t.code}</span></td>
              <td>{t.manager ? t.manager.name : <span className="pill bad">미지정</span>}</td>
              <td className="num">{t.counts.bus + t.counts.car}/{t.headcount}</td>
              <td className="num">{won(t.billed)}</td><td className="num">{won(t.final_fee)}</td>
              <td className="num">{won(t.received)}{t.received_proposed > 0 && <div className="muted">제안 {won(t.received_proposed)}</div>}</td>
              <td className="muted" style={{ minWidth: 180 }}><Reason t={t} /></td>
              <td className="num"><b>{signed(t.balance)}</b></td>
              <td><span className={`pill ${STATUS[t.status][1]}`}>{STATUS[t.status][0]}</span></td>
              <td><button className="small" onClick={() => setSel(sel === t.team_id ? null : t.team_id)}>{sel === t.team_id ? "닫기" : "명세"}</button></td>
            </tr>))}
          </tbody>
        </table></div>
        {teams.length === 0 && <p className="muted">해당하는 부서가 없습니다.</p>}
      </div>
      {sel && <TeamStatement base={base} teamId={sel} onChange={reload} />}

      <div className="card">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2>사람별 변동 (청구 이후)</h2>
          <div className="row seg">
            {["all", "extra", "refund", "pending", "rule_missing", "unresolved"].map((k) => (
              <button key={k} className={`small ${kind === k ? "active" : ""}`} onClick={() => setKind(k)}>
                {k === "all" ? `전체 ${rep.people.length}` : `${KIND[k][0]} ${s.people_counts[k] ?? 0}`}
              </button>
            ))}
          </div>
        </div>
        <p className="muted">납부 마감 청구표의 회비와 지금 회비가 다른 사람, 그리고 아직 확정되지 않은 사람입니다. 근거는 변동 기록(누가·언제·무엇에서 무엇으로·적용 규칙) 그대로입니다.</p>
        <div className="table-wrap"><table>
          <thead><tr><th>이름</th><th>부서</th><th>구분</th><th>청구 때 → 지금</th><th className="num">차액</th><th>변동 근거</th></tr></thead>
          <tbody>{people.map((p) => (
            <tr key={`${p.participant_id}-${p.kind}`}>
              <td><b>{p.name}</b> {p.on_event_day && <span className="pill warn">당일</span>}</td>
              <td>{p.team_name}</td>
              <td><span className={`pill ${KIND[p.kind][1]}`}>{KIND[p.kind][0]}</span></td>
              <td>{CHOICE_LABEL[p.billed_choice ?? "none"]} {won(p.billed_fee)} → {CHOICE_LABEL[p.choice ?? "none"]} {won(p.final_fee)}</td>
              <td className="num">{p.amount ? (p.kind === "refund" ? `−${won(p.amount)}` : `+${won(p.amount)}`) : "-"}</td>
              <td className="muted">{p.changes.length === 0 ? "응답 없음 — 총무 확인 필요" : p.changes.map((c) => (
                <div key={c.change_id}>{c.from} → {c.to} · {c.source} · {fmtTime(c.requested_at)} {c.status === "pending_approval" && <StatusPill status={c.status} />}<br /><span>{c.rule}{c.note ? ` · ${c.note}` : ""}</span></div>
              ))}</td>
            </tr>))}
          </tbody>
        </table></div>
        {people.length === 0 && <p className="muted">해당하는 사람이 없습니다.</p>}
      </div>

      {rep.versions && rep.versions.length > 1 && (
        <details className="card">
          <summary><b>보고서 버전 기록</b> <span className="muted">{rep.versions.length}개 — 기준표나 변동 기록이 바뀔 때마다 정산 Agent 가 새 버전을 만듭니다</span></summary>
          <ul style={{ margin: "8px 0 0", paddingLeft: 18 }}>
            {rep.versions.map((v) => <li key={v.version}>v{v.version} · {fmtTime(v.created_at)} · {triggerLabel(v.trigger)}</li>)}
          </ul>
        </details>
      )}
    </>
  );
}

function Reason({ t }: { t: ReportTeam }) {
  const parts: string[] = [];
  if (t.billed === null) parts.push("납부 마감 전(청구표 없음)");
  if (t.payment_gap) parts.push(t.payment_gap > 0 ? `청구보다 ${won(t.payment_gap)} 덜 입금` : `청구보다 ${won(-t.payment_gap)} 더 입금`);
  if (t.change_delta) parts.push(`마감 후 변동 ${signed(t.change_delta)}`);
  if (t.refunds_paid) parts.push(`환불 지급 ${won(t.refunds_paid)}`);
  if (t.late_paid > 0 && t.billed !== null) parts.push(`마감 후 입금 ${won(t.late_paid)}`);
  return (
    <>
      {parts.length ? parts.join(" · ") : t.status === "done" ? "차이 없음" : ""}
      {t.blockers.length > 0 && <div className="error" style={{ fontSize: 12 }}>{t.blockers.join(" · ")}</div>}
    </>
  );
}

function Kpi({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: string }) {
  return (
    <div className={`stat ${tone ? `tone-${tone}` : ""}`}>
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {sub && <div className="muted" style={{ fontSize: 12 }}>{sub}</div>}
    </div>
  );
}

/** 무엇이 언제 저장됐나: 참석 마감(응답 기준표) → 납부 마감(청구표) → 행사 → 보고서 */
export function Timeline({ rep }: { rep: Report }) {
  const ev = rep.event;
  const order = ["collecting", "attend_closed", "payment_closed", "event_done"];
  const at = order.indexOf(rep.summary.stage);
  const steps = [
    { title: "참석 마감", when: ev?.attend_deadline, saved: rep.summary.checkpoints.attend_snapshot_at, what: "사람별 응답 기준표 저장" },
    { title: "납부 마감", when: ev?.payment_deadline, saved: rep.summary.checkpoints.payment_billing_at, what: "부서별 청구표 저장" },
    { title: "행사", when: ev?.event_at, saved: null, what: "당일 변동은 총무가 입력" },
    { title: "정산 보고서", when: rep.created_at, saved: rep.created_at, what: `v${rep.version} · 변동 기록과 대조` },
  ];
  return (
    <ol className="timeline" aria-label="정산 단계">
      {steps.map((st, i) => (
        <li key={st.title} className={i <= at || (i === 3 && at === 3) ? "done" : i === at + 1 ? "now" : ""}>
          <b>{st.title}</b>
          <span className="muted">{fmtTime(st.when)}</span>
          <span className="muted">{st.what}{i < 2 && (st.saved ? " ✓" : " · 아직")}</span>
        </li>
      ))}
    </ol>
  );
}

function Diff({ rep }: { rep: Report }) {
  const d = rep.diff;
  if (!d || d.first) return null;
  const nt = d.teams?.length ?? 0, na = d.people_added?.length ?? 0, nr = d.people_removed?.length ?? 0;
  if (!nt && !na && !nr) return null;
  return (
    <div className="card">
      <h2>지난 버전(v{rep.version - 1}) 이후 바뀐 것 <span className="muted">· 계기: {triggerLabel(rep.trigger)}</span></h2>
      <div className="row" style={{ alignItems: "flex-start", gap: 18 }}>
        {nt > 0 && <ul style={{ margin: 0, paddingLeft: 18 }}>{d.teams!.map((t) => <li key={t.team_id}>{t.name}: {t.before ? `${signed(t.before.balance)} ${STATUS[t.before.status][0]}` : "새 부서"} → <b>{signed(t.after.balance)} {STATUS[t.after.status][0]}</b></li>)}</ul>}
        {(na > 0 || nr > 0) && (
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {d.people_added!.map((p, i) => <li key={`a${i}`}>+ {p.name}({p.team}) {KIND[p.kind]?.[0] ?? p.kind} {p.amount ? won(p.amount) : ""}</li>)}
            {d.people_removed!.map((p, i) => <li key={`r${i}`} className="muted">− {p.name}({p.team}) {KIND[p.kind]?.[0] ?? p.kind} 해소</li>)}
          </ul>
        )}
      </div>
    </div>
  );
}
