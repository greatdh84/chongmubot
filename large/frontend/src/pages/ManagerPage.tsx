import { useState } from "react";
import { api, fmtTime, won, type Change, type EventInfo, type SettlementCalc, type Task } from "../api";
import { Chat, ChoicePill, Msg, Page, StatusPill } from "../components/common";
import { useAction, useLoad } from "../hooks";

interface Overview {
  manager: { id: number; name: string };
  team: { id: number; code: string; name: string };
  event: EventInfo;
  counts: Record<string, number>;
  billing: SettlementCalc;
  changes: (Change & { participant_name: string })[];
  tasks: Task[];
  confirmed_settlement: { id: number; version: number; balance: number; confirmed_at: string; manager_ack_at: string | null } | null;
}

export default function ManagerPage() {
  const { data, error, reload } = useLoad(() => api.get<Overview>("/api/team"));
  const { busy, message, run } = useAction();
  const [correction, setCorrection] = useState("");
  if (error) return <Page narrow><div className="card"><p className="error">{error.message}</p><a href="/manager/login">이메일 인증하기</a></div></Page>;
  if (!data) return <Page><p className="muted">불러오는 중…</p></Page>;
  const b = data.billing;
  const open = data.tasks.filter((t) => t.status === "open");
  const answer = (t: Task, option: string) => run(() => api.post<{ review: { summary: string } }>(`/api/team/tasks/${t.id}/answer`, { option }), (r) => `답변 기록 · ${r.review.summary}`).then(reload);

  return (
    <Page>
      <div className="topbar">
        <h1>{data.team.code} {data.team.name} · 담당자 {data.manager.name}</h1>
        <button onClick={() => api.post("/api/auth/manager/logout").then(() => (window.location.href = "/"))}>로그아웃</button>
      </div>
      <div className="grid" style={{ marginBottom: 14 }}>
        {(["bus", "car", "absent", "undecided", "none"] as const).map((k) => (
          <div className="stat" key={k}><div className="label"><ChoicePill choice={k === "none" ? null : k} /></div><div className="value">{data.counts[k]}명</div></div>
        ))}
      </div>
      <div className="grid" style={{ marginBottom: 14 }}>
        <div className="stat"><div className="label">최종 회비</div><div className="value">{won(b.final_fee)}</div></div>
        <div className="stat"><div className="label">확정 수납액</div><div className="value">{won(b.received)}</div></div>
        <div className="stat"><div className="label">지급한 환불</div><div className="value">{won(b.refunds_paid)}</div></div>
        <div className="stat"><div className="label">잔액 ({b.balance_label})</div><div className="value">{won(Math.abs(b.balance))}</div></div>
      </div>
      {b.blockers.length > 0 && <div className="notice warn">정산 확정 전 확인할 것: {b.blockers.join(" · ")}</div>}
      <Msg m={message} />

      <div className="two">
        <div>
          <div className="card">
            <h2>확인 요청 {open.length > 0 && <span className="pill warn">{open.length}건 대기</span>}</h2>
            {data.tasks.length === 0 && <p className="muted">받은 확인 요청이 없습니다.</p>}
            {data.tasks.map((t) => (
              <div key={t.id} style={{ borderBottom: "1px solid var(--line)", padding: "8px 0" }}>
                <div className="row"><b>#{t.id}</b><StatusPill status={t.status} /><span className="muted">{fmtTime(t.created_at)}</span></div>
                <p style={{ margin: "4px 0" }}>{t.question}</p>
                {t.status === "open" ? (
                  <div className="row">{t.options.map((o) => <button key={o.key} className="small" disabled={busy} onClick={() => answer(t, o.key)}>{o.label}</button>)}</div>
                ) : (
                  <p className="muted">답변: {t.options.find((o) => o.key === t.answer)?.label ?? "-"} {t.resolution && `→ ${t.resolution}`}</p>
                )}
              </div>
            ))}
          </div>
          <div className="card">
            <h2>정산 명세</h2>
            {data.confirmed_settlement ? (
              <>
                <p>총무 확정 v{data.confirmed_settlement.version} ({fmtTime(data.confirmed_settlement.confirmed_at)}) · 잔액 {won(Math.abs(data.confirmed_settlement.balance))} {data.confirmed_settlement.balance > 0 ? "추가 납부" : data.confirmed_settlement.balance < 0 ? "추가 환불" : ""}</p>
                {data.confirmed_settlement.manager_ack_at ? <p className="notice ok">{fmtTime(data.confirmed_settlement.manager_ack_at)} 내역 확인함</p> : (
                  <button className="primary" disabled={busy} onClick={() => run(() => api.post("/api/team/statement/ack"), "내역을 확인했습니다.").then(reload)}>내역 확인</button>
                )}
              </>
            ) : <p className="muted">총무가 아직 정산을 확정하지 않았습니다. 아래는 현재 계산값입니다.</p>}
            <h3>입금 내역</h3>
            <div className="table-wrap"><table>
              <thead><tr><th>일시</th><th>입금자</th><th>구분</th><th className="num">금액</th><th>상태</th></tr></thead>
              <tbody>{b.receipts.map((r) => <tr key={r.allocation_id}><td>{fmtTime(r.txn_at)}</td><td>{r.depositor}</td><td>{r.kind}</td><td className="num">{won(r.amount)}</td><td><StatusPill status={r.status} /></td></tr>)}</tbody>
            </table></div>
            <p className="muted">{b.rules.join(" · ")}</p>
            <h3>정정 요청</h3>
            <div className="row">
              <input style={{ flex: 1 }} value={correction} onChange={(e) => setCorrection(e.target.value)} placeholder="예: 10/3 입금 중 2만원은 다른 팀 몫입니다" aria-label="정정 요청" maxLength={500} />
              <button disabled={busy || !correction.trim()} onClick={() => run(() => api.post("/api/team/corrections", { text: correction }), "총무에게 정정 요청을 보냈습니다.").then(() => setCorrection(""))}>보내기</button>
            </div>
          </div>
        </div>
        <Chat base="/api/team/chat" title="Agent 와 확인 대화" examples={["확인 요청 보여줘", "우리 팀 정산 현황", "#12 네 맞아요"]} />
      </div>

      <div className="card">
        <h2>팀원별 청구 내역</h2>
        <div className="table-wrap"><table>
          <thead><tr><th>이름</th><th>마감 기준</th><th>현재</th><th className="num">회비</th><th>변경</th></tr></thead>
          <tbody>{b.lines.map((l) => (
            <tr key={l.participant_id}>
              <td>{l.name}</td><td><ChoicePill choice={l.snapshot_choice} /></td><td><ChoicePill choice={l.choice} /></td>
              <td className="num">{won(l.fee)}</td>
              <td className="muted">{l.changes.map((c) => `${c.status === "applied" ? "" : "[승인대기] "}${c.refund_amount ? `환불 ${won(c.refund_amount)}` : c.extra_charge ? `추가 ${won(c.extra_charge)}` : "변동 없음"}`).join(", ")}</td>
            </tr>))}
          </tbody>
        </table></div>
      </div>
    </Page>
  );
}
