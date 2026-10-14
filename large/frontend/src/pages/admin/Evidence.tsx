import { useState } from "react";
import { api, fmtTime, won } from "../../api";
import { Msg } from "../../components/common";
import { useAction, useLoad } from "../../hooks";

/** 화면 이동(탭 + 입금·확인 하위 탭) */
export type Go = (tab: string, sub?: string) => void;

export interface EvMessage {
  id: number | string; source: "demo_slack" | "dm_job"; sent_at: string; sender: string; recipient: string; team_id: number | null;
  participant_id: number | null; participant_name: string | null; text: string; kind_label?: string;
  analysis: { kind: string; kind_label: string; to?: string; confidence?: string; by?: string; outcome?: string; outcome_label?: string; change_id?: number; check?: string; reason?: string; suggested_refund?: number } | null;
}
interface EvChange {
  change_id: number; participant_id: number; name: string; from: string; to: string; status: string; delta: number | null; pending_amount_text: string | null;
  basis_kind: "rule" | "admin" | "pending"; basis: string; requested_at: string; decided_at: string | null; source: string; on_event_day: boolean; note: string;
  messages: { id: number; sender: string; sent_at: string; text: string; kind: string | null }[];
}
interface EvReceipt {
  transaction_id: number; allocation_id: number | null; txn_at: string; depositor: string; amount: number; txn_amount: number; memo: string; status: string; status_label: string;
  confidence: string; basis: string; participant_name: string | null; after_deadline: boolean; late_label: string | null; task_id?: number;
  slack_suggestion?: { team: string; sender: string; sent_at: string; text: string; why: string } | null;
}
export interface TeamEvidenceData {
  team: { team_id: number; code: string; name: string; headcount: number; manager: string | null };
  billing: { captured_at: string; deadline: string; parts: { label: string; n: number; fee: number }[]; absent: number; unresolved: number; unresolved_names: string[]; billed: number; formula: string } | null;
  before_billing: EvChange[]; changes: EvChange[]; final_fee: number; check: { billed: number; delta: number; final: number; ok: boolean } | null;
  received: number; refunds_paid: number; balance: number; receipts: EvReceipt[]; messages: EvMessage[];
  blockers: { text: string; task_id?: number; kind?: string; suggestion?: string | null; link: { tab: string; sub?: string; ids?: number[] } }[];
}

const signed = (v: number | null | undefined) => (v === null || v === undefined ? "-" : v === 0 ? "0원" : `${v > 0 ? "+" : "−"}${Math.abs(v).toLocaleString("ko-KR")}원`);
const BY: Record<string, [string, string]> = { rule: ["규칙", ""], ai: ["AI", "ai"], human: ["사람", "warn"], code: ["코드", "ok"], fallback: ["LLM 실패→규칙", "bad"] };
export const ByPill = ({ by }: { by: string }) => { const [l, c] = BY[by] ?? [by, ""]; return <span className={`pill ${c}`}>{l}</span>; };
const byOf = (b?: string) => (b?.startsWith("claude") || b?.startsWith("gemini") ? "ai" : b === "mock_fallback" ? "fallback" : "rule");

/** 슬랙 말풍선: 보낸 사람·일시, 정산 Agent 가 읽은 결과(감지·판단) */
export function SlackBubbles({ messages, highlight }: { messages: EvMessage[]; highlight?: string }) {
  if (!messages.length) return <p className="muted">관련 슬랙 대화가 없습니다.</p>;
  return (
    <div className="slack-log">
      {messages.map((m) => {
        const bot = m.sender === "총무봇" || m.sender === "총무";
        return (
          <div key={m.id} className={`sbubble ${bot ? "bot" : ""} ${highlight && m.text.includes(highlight) ? "hl" : ""}`}>
            <div className="who"><b>{m.sender}</b> → {m.recipient || "-"} <span className="muted">{fmtTime(m.sent_at)}</span>{m.source === "dm_job" && <span className="pill ok">보낸 DM · {m.kind_label}</span>}</div>
            <div className="txt">{m.text}</div>
            {m.analysis && m.analysis.kind !== "note" && (
              <div className="trace"><ByPill by={byOf(m.analysis.by)} /> {m.analysis.kind_label}{m.analysis.to ? ` → ${m.analysis.to}` : ""}{m.analysis.confidence ? ` · 확신도 ${m.analysis.confidence}` : ""} · {m.analysis.outcome_label}</div>
            )}
          </div>
        );
      })}
    </div>
  );
}

/** 정산 보고서 부서 행을 펼치면 나오는 근거 패널: ① 청구 → 최종 ② 입금 내역 ③ 슬랙 대화 ④ 확인 필요 사유 */
export function TeamEvidence({ base, teamId, go, onStatement }: { base: string; teamId: number; go?: Go; onStatement?: () => void }) {
  const { data: e } = useLoad(() => api.get<TeamEvidenceData>(`${base}/report/teams/${teamId}/evidence`), [base, teamId]);
  if (!e) return <p className="muted">근거를 모으는 중…</p>;
  const b = e.billing;
  const diff = b ? e.final_fee - b.billed : null;
  return (
    <div className="evidence">
      <section>
        <h3>① 청구 → 최종 계산</h3>
        {b ? (
          <div className="calc">
            <div className="calc-row"><span>청구 <span className="muted">(납부 마감 {fmtTime(b.deadline)} 청구표)</span></span><span>{b.parts.filter((x) => x.n).map((x) => `${x.label} ${x.n}×${x.fee.toLocaleString("ko-KR")}`).join(" + ") || "0명"}</span><b>{won(b.billed)}</b></div>
            {b.unresolved > 0 && <div className="calc-row muted"><span>청구 때 미확정(0원으로 셈)</span><span>{b.unresolved_names.join(", ")}</span><span /></div>}
            {e.before_billing.map((c) => <div key={c.change_id} className="calc-row muted"><span>청구 전 반영 · {c.name}</span><span>{c.from} → {c.to} · {c.source} {fmtTime(c.requested_at)}</span><span>청구에 포함</span></div>)}
            {e.changes.map((c) => (
              <div key={c.change_id} className={`calc-row change ${c.status === "pending_approval" ? "pending" : ""}`}>
                <span><b>{c.name}</b> {c.from} → {c.to} {c.on_event_day && <span className="pill warn">행사 당일</span>}</span>
                <span className="muted">
                  근거: <ByPill by={c.basis_kind === "admin" ? "human" : c.basis_kind === "pending" ? "human" : "rule"} /> {c.basis_kind === "admin" ? c.basis : c.basis_kind === "pending" ? "규칙 없음 → 총무 판단" : c.basis}
                  <br />요청 {fmtTime(c.requested_at)} · {c.source}{c.decided_at ? ` · 승인 ${fmtTime(c.decided_at)}` : " · 승인 대기"}
                  {c.messages[0] && <><br />슬랙 {c.messages[0].sender}: “{c.messages[0].text}”</>}
                </span>
                <b className={c.delta && c.delta > 0 ? "plus" : c.delta && c.delta < 0 ? "minus" : ""}>{c.status === "applied" ? signed(c.delta) : c.pending_amount_text}</b>
              </div>
            ))}
            {e.changes.length === 0 && <div className="calc-row muted"><span>마감 후 변동 없음</span><span /><span /></div>}
            <div className={`calc-row total ${diff ? "diff" : ""}`}><span>최종 회비</span><span>{diff ? <>청구 대비 <b>{signed(diff)}</b></> : "청구와 같음"}{e.check && !e.check.ok && <span className="pill bad">검산 불일치</span>}</span><b>{won(e.final_fee)}</b></div>
            <div className="calc-row"><span>확정 입금{e.refunds_paid ? ` · 지급 환불 ${won(e.refunds_paid)}` : ""}</span><span /><span>{won(e.received)}</span></div>
            <div className="calc-row total"><span>정산 잔액</span><span className="muted">{e.balance > 0 ? "담당자가 더 걷어 낼 돈" : e.balance < 0 ? "총무가 담당자에게 돌려줄 돈" : "정산 완료"}</span><b>{signed(e.balance)}</b></div>
          </div>
        ) : <p className="muted">납부 마감 전이라 청구표가 없습니다(최종 회비 {won(e.final_fee)}).</p>}
      </section>
      <section>
        <h3>② 입금 내역</h3>
        {e.receipts.length === 0 ? <p className="muted">배정된 입금이 없습니다.</p> : (
          <div className="table-wrap"><table>
            <thead><tr><th>거래일시</th><th>입금자명</th><th className="num">금액</th><th>메모</th><th>배정</th></tr></thead>
            <tbody>{e.receipts.map((r) => (
              <tr key={`${r.transaction_id}-${r.allocation_id ?? "t"}`}>
                <td>{fmtTime(r.txn_at)} {r.after_deadline && <span className={`pill ${r.late_label?.startsWith("추가 납부 완료") ? "ok" : "warn"}`}>{r.late_label ?? "납부 마감 후"}</span>}</td>
                <td>{r.depositor}{r.participant_name ? <span className="muted"> (개인 · {r.participant_name})</span> : ""}</td>
                <td className="num">{won(r.amount)}{r.txn_amount !== r.amount && <div className="muted">거래 {won(r.txn_amount)}</div>}</td>
                <td className="muted">{r.memo || "-"}</td>
                <td><span className={`pill ${r.status === "confirmed" ? "ok" : r.status === "proposed" ? "warn" : "bad"}`}>{r.status_label}</span> <span className="muted">{r.basis.slice(0, 60)}</span>
                  {r.slack_suggestion && <div className="muted">슬랙 근거 → {r.slack_suggestion.team} 후보 ({r.slack_suggestion.sender} “{r.slack_suggestion.text}”)</div>}</td>
              </tr>))}
            </tbody>
          </table></div>
        )}
      </section>
      <section>
        <h3>③ 슬랙 대화 <span className="muted">· 보낸 DM 포함, 시간순</span></h3>
        <SlackBubbles messages={e.messages} />
      </section>
      <section>
        <h3>④ 확인 필요 사유</h3>
        {e.blockers.length === 0 ? <p className="muted">없음 — 정산 확정 가능</p> : (
          <ul className="todo">{e.blockers.map((x, i) => (
            <li key={i}><span>{x.text}{x.suggestion && <> · <span className="pill ai">{x.suggestion}</span></>}</span>{go && <button className="small" onClick={() => go(x.link.tab, x.link.sub)}>{x.link.sub ?? x.link.tab} 열기</button>}</li>
          ))}</ul>
        )}
        {onStatement && <button className="small ghost" onClick={onStatement}>정산 확정·송금 명세 열기</button>}
      </section>
    </div>
  );
}

interface PreviewTarget { team_id: number; team: string; manager: string; amount: number; members: { name: string; change: string; amount: number }[]; subject: string; body: string; already_sent: boolean; job_status: string | null }
interface Preview { kind: string; targets: PreviewTarget[]; skipped_check: { team: string; amount: number; blockers: string[] }[]; no_manager: string[]; notify_mode: string }
interface Sent { id: number; kind: string; team: string; to: string; subject: string; body: string | null; sent_at: string; mode: string }

/** 담당자 안내: 미리보기(부서·담당자·금액·대상 팀원·실제 문구) → [보내기] → 담당자에게만 DM. 같은 금액 중복 발송은 서버가 막는다. */
export function NotifyPanel({ base, refundTeams, extraTeams, onSent }: { base: string; refundTeams: number; extraTeams: number; onSent?: () => void }) {
  const [pv, setPv] = useState<Preview | null>(null);
  const { busy, message, run } = useAction();
  const { data: sent, reload } = useLoad(() => api.get<Sent[]>(`${base}/emails/sent`).then((x) => x.filter((j) => j.kind === "refund_notice" || j.kind === "extra_notice")), [base]);
  const label = (k: string) => (k === "refund_notice" ? "환불 안내" : "추가 납부 안내");
  const open = (k: string) => run(() => api.post<Preview>(`${base}/emails/preview`, { kind: k }).then(setPv));
  const send = () => pv && run(async () => {
    const r = await api.post<{ created: number }>(`${base}/emails/enqueue`, { kind: pv.kind });
    const d = await api.post<{ sent?: number; deferred?: number; notify_mode: string }>(`${base}/emails/dispatch`);
    return { ...r, ...d };
  }, (r) => `${label(pv.kind)} ${r.created}건 생성 · 발송 ${r.sent ?? 0}건${r.deferred ? ` · 연기 ${r.deferred}건(21~8시 금지)` : ""}${r.notify_mode === "mock" ? " (모의 발송 — 아래 '보낸 메시지')" : ""}`).then(() => { setPv(null); reload(); onSent?.(); });
  return (
    <div className="notify">
      <div className="row">
        <button className="yellow" disabled={busy || !refundTeams} onClick={() => open("refund_notice")}>환불 부서 담당자에게 안내 ({refundTeams})</button>
        <button className="yellow" disabled={busy || !extraTeams} onClick={() => open("extra_notice")}>추가 납부 부서 담당자에게 안내 ({extraTeams})</button>
        <span className="muted">회비는 팀 단위 — 개인이 아니라 담당자에게만 갑니다. 먼저 미리보기가 열립니다.</span>
      </div>
      <Msg m={message} />
      {pv && (
        <div className="preview-box" role="dialog" aria-label={`${label(pv.kind)} 미리보기`}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <b>{label(pv.kind)} 미리보기 · {pv.targets.length}개 부서 담당자</b>
            <span className="pill">{pv.notify_mode === "mock" ? "Slack DM 모의(개발용 수신함)" : "Slack DM 실제 발송"}</span>
          </div>
          {pv.targets.map((t) => (
            <details key={t.team_id} className="pv-item" open={pv.targets.length <= 3}>
              <summary><b>{t.team}</b> · 담당자 {t.manager} · <b>{won(t.amount)}</b> {t.members.length > 0 && <span className="muted">· 대상 팀원 {t.members.map((m) => `${m.name}(${signed(m.amount)})`).join(", ")}</span>} {t.already_sent && <span className="pill ok">같은 금액으로 이미 보냄 — 다시 안 감</span>}</summary>
              <pre className="mail">{t.subject}{"\n\n"}{t.body}</pre>
            </details>
          ))}
          {pv.skipped_check.length > 0 && <p className="muted">확인 필요로 보류(보내지 않음): {pv.skipped_check.map((x) => x.team).join(", ")}</p>}
          {pv.no_manager.length > 0 && <p className="error">담당자 미지정(발송 안 됨): {pv.no_manager.join(", ")}</p>}
          <div className="row">
            <button className="yellow" disabled={busy || pv.targets.every((t) => t.already_sent)} onClick={send}>보내기</button>
            <button disabled={busy} onClick={() => setPv(null)}>취소</button>
          </div>
        </div>
      )}
      {sent && sent.length > 0 && (
        <details className="sent-list">
          <summary><b>보낸 메시지</b> <span className="muted">{sent.length}건 · {sent[0].mode === "mock" ? "모의 발송(실제 Slack 아님)" : "Slack DM"}</span></summary>
          {sent.map((j) => <div key={j.id} className="sent"><div className="row"><span className="pill ok">{label(j.kind)}</span><b>{j.team}</b> → 담당자 {j.to} <span className="muted">{fmtTime(j.sent_at)}</span></div>{j.body && <pre className="mail">{j.body}</pre>}</div>)}
        </details>
      )}
    </div>
  );
}

export interface TimelineItem { at: string; title: string; steps: { phase: string; text: string; by: string }[]; message_id?: number; change_id?: number }

/** Agent 활동 기록: 감지 → 판단 → 실행 → 사람에게 넘김 (원래 시각 기준) */
export function AgentTimeline({ base }: { base: string }) {
  const { data } = useLoad(() => api.get<TimelineItem[]>(`${base}/agent/timeline`), [base]);
  const [all, setAll] = useState(false);
  if (!data) return null;
  const rows = all ? data : data.slice(-8);
  return (
    <div className="card">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2>Agent 활동 기록 <span className="muted">감지 → 판단 → 실행 → 사람에게 넘김</span></h2>
        {data.length > 8 && <button className="small ghost" onClick={() => setAll(!all)}>{all ? "최근만" : `전체 ${data.length}건`}</button>}
      </div>
      {data.length === 0 && <p className="muted">아직 기록이 없습니다.</p>}
      <ol className="agent-tl">
        {rows.map((x, i) => (
          <li key={i}>
            <div className="when">{fmtTime(x.at)}</div>
            <div>
              <b>{x.title}</b>
              <div className="chain">{x.steps.map((s, j) => (
                <span key={j} className="step"><span className={`ph ph-${s.phase}`}>{s.phase}</span> <ByPill by={s.by} /> {s.text}</span>
              ))}</div>
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}

interface CardItem { transaction_id: number; txn_at: string; depositor: string; amount: number; memo: string; teams: string[]; label: string; state: string; by: string; why: string; task_ids: number[] }
export interface SettlementCardData { total: number; auto: number; human: number; human_done: number; rate: number; reasons: Record<string, number>; items: CardItem[]; last_run: { at: string; summary: string; rules: string[] } | null; slack: { at: string; summary: string; by: string; fallback: string } | null }

/** 정산 Agent 실행 카드: 거래 n건 읽음 → 자동 처리 → 사람 확인(사유별), 판단마다 규칙/AI/사람 라벨과 근거 */
export function SettlementRunCard({ base, go }: { base: string; go?: Go }) {
  const { data: c } = useLoad(() => api.get<SettlementCardData>(`${base}/agent/settlement-card`), [base]);
  if (!c) return null;
  const human = c.items.filter((i) => i.state === "human");
  return (
    <div className="card">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2>정산 Agent 실행</h2>
        {go && <button className="small" onClick={() => go("입금·확인", "입금 대조")}>입금 대조 열기</button>}
      </div>
      {c.slack && (
        <p style={{ margin: "0 0 8px" }}><ByPill by={byOf(c.slack.by)} /> {c.slack.summary}{c.slack.fallback && <span className="pill bad" title={c.slack.fallback}>LLM 실패 → 규칙</span>}</p>
      )}
      {c.total === 0 ? <p className="muted">아직 거래내역이 없습니다. 입금·확인에서 거래내역 CSV 를 올리고 정산 Agent 점검을 실행하세요.</p> : (
        <>
          <div className="flow">
            <span className="fstep">거래 <b>{c.total}</b>건 읽음</span><span className="arrow">→</span>
            <span className="fstep ok"><b>{c.auto}</b>건 자동 확정 <span className="muted">(높은 확신·규칙 R1)</span></span><span className="arrow">→</span>
            <span className="fstep warn"><b>{c.human}</b>건 사람 확인{Object.keys(c.reasons).length > 0 && <span className="muted"> ({Object.entries(c.reasons).map(([k, v]) => `${k} ${v}`).join(" · ")})</span>}</span>
            {c.human_done > 0 && <span className="fstep">사람 확인 완료 {c.human_done}건</span>}
          </div>
          <div className="meter" aria-label={`자동 처리율 ${c.rate}%`}><span style={{ width: `${c.rate}%` }} /></div>
          <p className="muted" style={{ marginTop: 4 }}>자동 처리율 <b>{c.rate}%</b> ({c.auto}/{c.total}) · 확정은 총무 일괄 확정(1클릭), 나머지는 사람이 근거를 보고 정함{c.last_run ? ` · 마지막 점검 ${fmtTime(c.last_run.at)}` : ""}</p>
          {human.length > 0 && (
            <ul className="todo">{human.map((i) => (
              <li key={i.transaction_id}>
                <span><span className="pill warn">{i.label}</span> <ByPill by={i.by} /> <b>{i.depositor}</b> {won(i.amount)} <span className="muted">{fmtTime(i.txn_at)} · {i.why.slice(0, 110)}</span></span>
                {go && <button className="small" onClick={() => go("입금·확인", i.label === "제안 확인" ? "입금 대조" : "확인 과제")}>보기</button>}
              </li>
            ))}</ul>
          )}
        </>
      )}
    </div>
  );
}
