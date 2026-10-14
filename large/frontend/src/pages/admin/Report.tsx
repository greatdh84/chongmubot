import { Fragment, useState } from "react";
import { api, CHOICE_LABEL, fmtTime, STATIC, won } from "../../api";
import { Msg } from "../../components/common";
import { useAction, useLoad } from "../../hooks";
import { TeamStatement } from "./AdminMoney";
import { NotifyPanel, TeamEvidence, type Go } from "./Evidence";

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
  kind: "extra" | "refund" | "pending" | "unresolved" | "rule_missing" | "changed" | "included";
  snapshot_choice: string | null; billed_choice: string | null; choice: string | null;
  billed_fee: number | null; final_fee: number | null; amount: number; on_event_day: boolean;
  manager_name?: string | null; basis_kind?: "rule" | "admin" | "pending" | "none"; basis?: string; process?: string;
  slack?: { at: string; sender: string; text: string; id: number } | null;
  changes: { change_id: number; from: string; to: string; status: string; requested_at: string; decided_at?: string | null; source: string; rule: string; refund_amount: number; extra_charge: number; note: string; on_event_day: boolean }[];
}
export interface ReportSummary {
  stage: string; stage_label: string; checkpoints: { attend_snapshot_at: string | null; payment_billing_at: string | null };
  headcount: number; attending: number; counts: Record<string, number>; teams: number;
  billed_total: number | null; final_total: number; received_total: number; refunds_paid_total: number;
  extra_total: number; refund_total: number; check_total: number;
  team_status: Record<string, number>; people_counts: Record<string, number>; on_event_day_changes: number;
  deadline_counts?: Record<string, number> | null; billed_final_delta?: number | null;
}
export interface Report {
  id: number; version: number; trigger: string; stage: string; created_at: string; summary: ReportSummary; teams: ReportTeam[]; people: ReportPerson[];
  diff: { first?: boolean; teams?: { team_id: number; name: string; before: { balance: number; status: string } | null; after: { balance: number; status: string } }[]; people_added?: { name: string; team: string; kind: string; amount: number }[]; people_removed?: { name: string; team: string; kind: string; amount: number }[] };
  versions?: { version: number; trigger: string; created_at: string; stage: string }[]; diff_base?: number;
  event?: { attend_deadline: string; payment_deadline: string; event_at: string };
}

export const STATUS: Record<string, [string, string]> = { extra: ["추가 납부", "warn"], refund: ["환불", "info"], check: ["확인 필요", "bad"], done: ["정산 완료", "ok"] };
export const KIND: Record<string, [string, string]> = {
  extra: ["추가 납부", "warn"], refund: ["환불", "info"], pending: ["승인 대기", "bad"], unresolved: ["응답 미확정", "bad"], rule_missing: ["환불액 미정", "bad"], changed: ["변경 있음", ""],
  included: ["청구에 포함", "ok"],
};
const TRIGGER: Record<string, string> = {
  seed: "시드", admin_agent: "총무 Agent 질문", report_view: "보고서 열람", checkpoint: "마감 기준표 저장", scheduler_checkpoint: "마감 기준표 저장(스케줄러)",
  change_approved: "변경 승인", change_rejected: "변경 반려", transactions_upload: "거래내역 업로드", allocation_confirmed: "입금 배정 확정",
  allocations_confirmed_high: "입금 배정 일괄 확정", allocation_cancelled: "입금 배정 취소", allocation_manual: "수동 배정", settlement_review: "정산 Agent 점검",
  task_resolved: "확인 과제 처리", settlement_confirmed: "정산 확정", payout_recorded: "환불 송금 기록", admin_response: "총무 직접 입력",
  manager_answer: "담당자 답변", manager_chat: "담당자 채팅", participant_change: "참가자 변경", participant_chat: "참가자 채팅", roster_upload: "명단 업로드", manager_assigned: "담당자 지정",
  demo_responses: "시연 응답 CSV", demo_changes: "시연 변경 CSV", slack_import: "슬랙 대화 CSV", slack_suggestion_accepted: "슬랙 근거로 입금 지정",
};
export const triggerLabel = (t: string) => TRIGGER[t] ?? (t.startsWith("manual:") ? "총무가 다시 만들기" : t);
const signed = (v: number | null) => (v === null ? "-" : v === 0 ? "0원" : `${v > 0 ? "+" : "−"}${Math.abs(v).toLocaleString("ko-KR")}원`);

const seenKey = (base: string) => `large:report-seen:${base}`;
const readSeen = (base: string) => { try { const v = Number(localStorage.getItem(seenKey(base))); return v > 0 ? v : null; } catch { return null; } };
const writeSeen = (base: string, v: number) => { try { localStorage.setItem(seenKey(base), String(v)); } catch { /* 저장 못 해도 화면은 그대로 */ } };

const PROCESS_TONE = (p: string) => (p.startsWith("팀 납부 완료") || p.startsWith("팀 정산") || p.startsWith("환불 완료") || p.startsWith("청구에") ? "ok" : p.includes("대기") ? "warn" : p ? "bad" : "");

export function ReportTab({ base, go }: { base: string; go?: Go }) {
  // 비교 기준: 이 화면에서 마지막으로 본 버전(바뀐 부서를 모아 보여 줌). 처음이면 바로 앞 버전.
  const [since, setSince] = useState<number | null>(() => readSeen(base));
  const { data: rep, reload } = useLoad(() => api.get<Report>(`${base}/report${since ? `?since=${since}` : ""}`).then((r) => { writeSeen(base, r.version); return r; }), [base, since]);
  const [filter, setFilter] = useState<string>("all");
  const [kind, setKind] = useState<string>("all");
  const [open, setOpen] = useState<number | null>(null);
  const [sel, setSel] = useState<number | null>(null);
  const { busy, message, run } = useAction();
  if (!rep) return <p className="muted">정산 보고서를 만드는 중…</p>;
  const s = rep.summary;
  const teams = rep.teams.filter((t) => filter === "all" || t.status === filter);
  const people = rep.people.filter((p) => kind === "all" || p.kind === kind);
  const dc = s.deadline_counts;
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
        <div className="headline">
          <div className="money">
            청구 합계 <b>{won(s.billed_total)}</b> <span className="arrow">→</span> 최종 합계 <b>{won(s.final_total)}</b>
            {s.billed_final_delta !== null && s.billed_final_delta !== undefined && <span className={`pill ${s.billed_final_delta ? "warn" : "ok"}`}>차이 {signed(s.billed_final_delta)}</span>}
            <span className="muted"> · 확정 입금 {won(s.received_total)}</span>
          </div>
          <div className="row">
            {(["extra", "refund", "check", "done"] as const).map((k) => (
              <button key={k} className={`small status-chip tone-${STATUS[k][1]} ${filter === k ? "active" : ""}`} onClick={() => setFilter(filter === k ? "all" : k)}>{STATUS[k][0]} <b>{s.team_status[k] ?? 0}</b></button>
            ))}
          </div>
          <span className="muted">
            {dc ? <>참석 마감 기준 버스 {dc.bus} · 자차 {dc.car} · 불참 {dc.absent} · 미정 {dc.undecided} · 미응답 {dc.none} / </> : null}
            현재 참석 {s.attending}명(전체 {s.headcount}명) · 행사 당일 변동 {s.on_event_day_changes}명
          </span>
        </div>
        <NotifyPanel base={base} refundTeams={s.team_status.refund ?? 0} extraTeams={s.team_status.extra ?? 0} onSent={reload} />
        <Timeline rep={rep} />
      </div>

      <Diff rep={rep} onBase={(v) => setSince(v)} />

      <div className="card">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2>부서별 <span className="muted">행을 누르면 근거(청구 → 최종 · 입금 · 슬랙 · 확인 필요)</span></h2>
          <div className="row seg" role="tablist" aria-label="상태 필터">
            {["all", "extra", "refund", "check", "done"].map((k) => (
              <button key={k} className={`small ${filter === k ? "active" : ""}`} onClick={() => setFilter(k)}>
                {k === "all" ? `전체 ${rep.teams.length}` : `${STATUS[k][0]} ${s.team_status[k] ?? 0}`}
              </button>
            ))}
          </div>
        </div>
        <div className="table-wrap"><table>
          <thead><tr><th>부서</th><th>담당자</th><th className="num">참석</th><th className="num">청구액</th><th className="num">최종 회비</th><th className="num">확정 입금</th><th>차이 이유</th><th className="num">정산 잔액</th><th>상태</th></tr></thead>
          <tbody>{teams.map((t) => (
            <Fragment key={t.team_id}>
              <tr className={`clickable ${open === t.team_id ? "sel" : ""} ${changedTeams(rep).has(t.team_id) ? "changed" : ""}`} onClick={() => setOpen(open === t.team_id ? null : t.team_id)} aria-expanded={open === t.team_id}>
                <td><span className="caret">{open === t.team_id ? "▾" : "▸"}</span> <b>{t.name}</b> <span className="muted">{t.code}</span>{changedTeams(rep).has(t.team_id) && <span className="pill info">바뀜</span>}</td>
                <td>{t.manager ? t.manager.name : <span className="pill bad">미지정</span>}</td>
                <td className="num">{t.counts.bus + t.counts.car}/{t.headcount}</td>
                <td className="num">{won(t.billed)}</td>
                <td className="num">{t.billed !== null && t.final_fee !== t.billed ? <b className="diffv">{won(t.final_fee)}</b> : won(t.final_fee)}</td>
                <td className="num">{won(t.received)}{t.received_proposed > 0 && <div className="muted">제안 {won(t.received_proposed)}</div>}</td>
                <td className="muted" style={{ minWidth: 180 }}><Reason t={t} /></td>
                <td className="num"><b>{signed(t.balance)}</b></td>
                <td><span className={`pill ${STATUS[t.status][1]}`}>{STATUS[t.status][0]}</span></td>
              </tr>
              {open === t.team_id && (
                <tr className="ev-row"><td colSpan={9}><TeamEvidence base={base} teamId={t.team_id} go={go} onStatement={() => setSel(t.team_id)} /></td></tr>
              )}
            </Fragment>))}
          </tbody>
        </table></div>
        {teams.length === 0 && <p className="muted">해당하는 부서가 없습니다.</p>}
      </div>
      {sel && <TeamStatement base={base} teamId={sel} onChange={reload} />}

      <div className="card">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2>사람별 변동 <span className="muted">참석 마감 이후 모든 변동</span></h2>
          <div className="row seg">
            {["all", "extra", "refund", "pending", "rule_missing", "unresolved", "included"].map((k) => (
              <button key={k} className={`small ${kind === k ? "active" : ""}`} onClick={() => setKind(k)}>
                {k === "all" ? `전체 ${rep.people.length}` : `${KIND[k][0]} ${s.people_counts[k] ?? 0}`}
              </button>
            ))}
          </div>
        </div>
        <p className="muted">추가·환불·승인 대기·미응답, 납부 마감 전에 반영돼 청구에 들어간 변동까지. 근거는 변동 기록(규칙 또는 총무 결정)과 관련 슬랙 대화 그대로입니다.</p>
        <div className="table-wrap"><table className="people">
          <thead><tr><th>부서(담당자)</th><th>이름</th><th>변동(전→후)</th><th>시점</th><th className="num">금액</th><th>근거</th><th>슬랙 한 줄</th><th>처리 상태</th></tr></thead>
          <tbody>{people.map((p) => {
            const last = p.changes[p.changes.length - 1];
            return (
              <tr key={`${p.participant_id}-${p.kind}`}>
                <td>{p.team_name} <span className="muted">({p.manager_name ?? "미지정"})</span></td>
                <td><b>{p.name}</b> <span className={`pill ${KIND[p.kind][1]}`}>{KIND[p.kind][0]}</span></td>
                <td>{last ? `${last.from} → ${last.to}` : `${CHOICE_LABEL[p.snapshot_choice ?? "none"]} (응답 없음)`}{last && <div className="muted">{last.source} · 요청 {fmtTime(last.requested_at)}{last.decided_at ? ` · 승인 ${fmtTime(last.decided_at)}` : ""}</div>}</td>
                <td>{p.on_event_day ? <span className="pill warn">행사 당일</span> : <span className="pill">일반</span>}</td>
                <td className="num">{p.kind === "included" ? <span className="muted">청구 포함</span> : p.amount ? <b className={p.kind === "refund" ? "minus" : "plus"}>{p.kind === "refund" ? "−" : "+"}{won(p.amount)}</b> : p.kind === "pending" ? <span className="muted">승인 후 결정</span> : "-"}</td>
                <td className="muted">{p.basis_kind === "admin" ? <span className="pill warn">총무 결정</span> : p.basis_kind === "pending" ? <span className="pill bad">규칙 없음</span> : p.basis_kind === "rule" ? <span className="pill">규칙</span> : null} {p.basis}</td>
                <td className="muted slack1">{p.slack ? <><b>{p.slack.sender}</b> {fmtTime(p.slack.at)}<br />“{p.slack.text}”</> : "-"}</td>
                <td>{p.process && <span className={`pill ${PROCESS_TONE(p.process)}`}>{p.process}</span>}</td>
              </tr>
            );
          })}
          </tbody>
        </table></div>
        {people.length === 0 && <p className="muted">해당하는 사람이 없습니다.</p>}
      </div>

      {rep.versions && rep.versions.length > 1 && (
        <details className="card">
          <summary><b>보고서 버전 기록</b> <span className="muted">{rep.versions.length}개 — 기준표나 변동 기록이 바뀔 때마다 정산 Agent 가 새 버전을 만듭니다</span></summary>
          <ul style={{ margin: "8px 0 0", paddingLeft: 18 }}>
            {rep.versions.map((v) => <li key={v.version}>v{v.version} · {fmtTime(v.created_at)} · {triggerLabel(v.trigger)} {v.version < rep.version && <button className="small ghost" onClick={() => setSince(v.version)}>이 버전과 비교</button>}</li>)}
          </ul>
        </details>
      )}
    </>
  );
}

const changedTeams = (rep: Report) => new Set((rep.diff?.first ? [] : rep.diff?.teams ?? []).map((t) => t.team_id));

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

export function Kpi({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: string }) {
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

function Diff({ rep, onBase }: { rep: Report; onBase: (v: number | null) => void }) {
  const d = rep.diff;
  if (!d || d.first) return null;
  const nt = d.teams?.length ?? 0, na = d.people_added?.length ?? 0, nr = d.people_removed?.length ?? 0;
  const baseV = rep.diff_base ?? rep.version - 1;
  if (!nt && !na && !nr) return null;
  return (
    <div className="card">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2>v{baseV} 이후 바뀐 부서 {nt > 0 && <span className="muted">· {d.teams!.map((t) => t.name).join(" · ")}</span>}</h2>
        {baseV !== rep.version - 1 && <button className="small ghost" onClick={() => onBase(rep.version - 1)}>바로 앞 버전(v{rep.version - 1})과 비교</button>}
      </div>
      <p className="muted" style={{ marginTop: 0 }}>{baseV === rep.version - 1 ? `계기: ${triggerLabel(rep.trigger)}` : `이 화면에서 마지막으로 본 v${baseV} 부터 지금 v${rep.version} 까지 모은 차이`}</p>
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
