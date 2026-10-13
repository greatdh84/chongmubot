import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, CHOICE_LABEL, fmtTime, won, type Choice, type Me } from "../api";
import { Chat, ChoicePill, Msg, Page, StatusPill, useEventName } from "../components/common";
import { useAction, useLoad } from "../hooks";

const OPTIONS: Choice[] = ["bus", "car", "absent", "undecided"];

interface Preview { needs_admin_rule?: boolean; after_deadline: boolean; requires_approval: boolean; fee_before: number | null; fee_after: number | null; refund_amount: number; extra_charge: number; rule_text: string }

export default function ParticipantPage() {
  const { data: me, error, reload } = useLoad(() => api.get<Me>("/api/large/me"));
  const [sel, setSel] = useState<Choice | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [pvError, setPvError] = useState("");
  const { busy, message, run } = useAction();
  useEventName(me?.event.name);

  useEffect(() => {
    setPreview(null);
    setPvError("");
    if (!sel || !me || sel === me.response.choice) return;
    api.post<Preview>("/api/large/me/response/preview", { choice: sel }).then(setPreview).catch((e) => setPvError(e.message));
  }, [sel, me]);

  if (error) return <Page narrow><div className="card"><p className="error">{error.message}</p><p className="muted">이메일의 개인 초대 링크로 다시 들어와 주세요.</p></div></Page>;
  if (!me) return <Page narrow><p className="muted">불러오는 중…</p></Page>;
  const ev = me.event;
  const submit = () =>
    run(async () => {
      const r = await api.post<{ change: { status: string } }>("/api/large/me/response", { choice: sel });
      setSel(null);
      reload();
      return r;
    }, (r) => (r.change.status === "applied" ? "응답을 저장했습니다." : "변경 요청을 접수했습니다. 총무 승인 후 반영됩니다."));

  return (
    <Page narrow>
      <div className="topbar">
        <h1>{me.participant.name}님 · {me.participant.team.name}</h1>
        {me.is_manager && <Link className="btn" to="/manager/login">담당자 화면</Link>}
      </div>
      <div className="card">
        <h2>{ev.name}</h2>
        <p className="muted">참석 응답 마감 {fmtTime(ev.attend_deadline)} · 팀 입금 마감 {fmtTime(ev.payment_deadline)}</p>
        <p>현재 응답: <ChoicePill choice={me.response.choice} /> · 회비 {won(me.response.fee)}</p>
        {me.pending_change && (
          <div className="notice warn">승인 대기 중인 변경: {me.pending_change.from_label} → {me.pending_change.to_label} (요청 {fmtTime(me.pending_change.requested_at)})</div>
        )}
      </div>

      <div className="card">
        <h2>참석 응답 {ev.after_attend_deadline ? "변경 요청" : "선택"}</h2>
        {ev.after_attend_deadline && <p className="notice warn">참석 마감이 지나 변경은 총무 승인 후 반영되며, 환불은 규칙에 따라 달라집니다(무조건 환불되지 않음).</p>}
        <div className="choices" role="radiogroup" aria-label="참석 응답">
          {OPTIONS.map((c) => (
            <button key={c} role="radio" aria-checked={(sel ?? me.response.choice) === c} className={`choice ${(sel ?? me.response.choice) === c ? "selected" : ""}`} onClick={() => setSel(c)} disabled={!!me.pending_change}>
              <div><b>{CHOICE_LABEL[c]}</b></div>
              <div className="fee">{c === "undecided" ? "회비 미확정" : won(ev.fees[c as "bus" | "car" | "absent"])}</div>
            </button>
          ))}
        </div>
        {preview && (
          <div className="notice">
            {preview.needs_admin_rule ? "환불액 미정 — 이 시점에 맞는 환불 규칙이 없어 총무가 정합니다(0원으로 확정된 것이 아님)" : `${won(preview.fee_before)} → ${won(preview.fee_after)}`}
            {preview.refund_amount > 0 && ` · 환불 ${won(preview.refund_amount)}`}
            {preview.extra_charge > 0 && ` · 추가 ${won(preview.extra_charge)}`}
            <div className="muted">규칙: {preview.rule_text}</div>
          </div>
        )}
        {pvError && <p className="error">{pvError}</p>}
        <div className="row" style={{ marginTop: 10 }}>
          <button className="primary" disabled={busy || !sel || sel === me.response.choice || !!pvError} onClick={submit}>
            {ev.after_attend_deadline ? "변경 요청 제출" : "제출"}
          </button>
          {sel && <button onClick={() => setSel(null)}>취소</button>}
        </div>
        <Msg m={message} />
      </div>

      <Chat base="/api/large/me/chat" title="Agent 에게 변경 요청·문의" examples={["버스 말고 자차로 갈게요", "제 회비 얼마예요?", "못 가게 됐어요"]} />

      <div className="card">
        <h2>변경 이력</h2>
        {me.changes.length === 0 ? <p className="muted">아직 기록이 없습니다.</p> : (
          <div className="table-wrap"><table>
            <thead><tr><th>요청</th><th>변경</th><th>상태</th><th className="num">환불/추가</th><th>규칙</th></tr></thead>
            <tbody>{me.changes.map((c) => (
              <tr key={c.id}>
                <td>{fmtTime(c.requested_at)}</td><td>{c.from_label} → {c.to_label}</td><td><StatusPill status={c.status} /></td>
                <td className="num">{c.refund_amount ? `-${won(c.refund_amount)}` : c.extra_charge ? `+${won(c.extra_charge)}` : "-"}</td>
                <td className="muted">{c.after_deadline ? c.rule_text : "마감 전"}</td>
              </tr>))}
            </tbody>
          </table></div>
        )}
        <p className="muted">로그인 이메일: {me.participant.email_masked}</p>
      </div>
    </Page>
  );
}
