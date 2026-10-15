import { useEffect, useState, type FormEvent } from "react";
import { api, ApiError, fmtTime, won } from "../api";
import { Msg, Page, useEventName } from "../components/common";
import { useAction, useLoad } from "../hooks";

/** 환불 받을 계좌(뒤 4자리만 보임). 전체 번호는 총무 화면의 [전체 보기]에서만. */
export interface MaskedAccount { id: number; bank: string; masked: string; holder: string; status: string; source: string; source_label: string; submitted_at: string; submitted_by?: string | null; confirmed_at: string | null; message_id: number | null }

const BANKS = ["시연은행", "국민", "신한", "우리", "하나", "농협", "기업", "카카오뱅크", "토스뱅크", "케이뱅크", "우체국", "새마을금고"];
const FIELD: Record<string, string> = { bank: "은행", account_no: "계좌번호", holder: "예금주" };

/** 은행·계좌번호·예금주 입력(형식 검사는 서버가 다시 함) */
function AccountForm({ onSubmit, busy, defaultHolder, demoNote }: { onSubmit: (v: { bank: string; account_no: string; holder: string }) => Promise<unknown>; busy: boolean; defaultHolder?: string; demoNote?: string }) {
  const [bank, setBank] = useState("시연은행");
  const [no, setNo] = useState("");
  const [holder, setHolder] = useState(defaultHolder ?? "");
  const [errs, setErrs] = useState<Record<string, string>>({});
  const [formErr, setFormErr] = useState("");
  const local = () => {
    const e: Record<string, string> = {};
    const d = no.replace(/\D/g, "");
    if (!/^[\d-]+$/.test(no) || d.length < 10 || d.length > 16) e.account_no = "숫자와 하이픈(-)만, 숫자 10~16자리";
    if (bank.trim().length < 2) e.bank = "은행을 골라 주세요";
    if (holder.trim().length < 2) e.holder = "예금주 2자 이상";
    return e;
  };
  const submit = async (ev: FormEvent) => {
    ev.preventDefault();
    const e = local();
    setErrs(e);
    setFormErr("");
    if (Object.keys(e).length) return;
    try {
      await onSubmit({ bank, account_no: no, holder });
      setNo("");
    } catch (x) {
      const d = (x as ApiError).detail;
      setFormErr((x as ApiError).message);
      if (Array.isArray(d)) setErrs(Object.fromEntries((d as { field: string; error: string }[]).filter((y) => y.field).map((y) => [y.field, y.error])));
    }
  };
  return (
    <form className="acct-form" onSubmit={submit}>
      {demoNote && <div className="notice warn">⚠ {demoNote}</div>}
      <label className="field">은행
        <select value={bank} onChange={(e) => setBank(e.target.value)} aria-invalid={!!errs.bank}>{BANKS.map((b) => <option key={b}>{b}</option>)}</select>
        {errs.bank && <span className="error">{errs.bank}</span>}
      </label>
      <label className="field">계좌번호
        <input value={no} onChange={(e) => setNo(e.target.value)} inputMode="numeric" autoComplete="off" placeholder="000-0000-0001" maxLength={30} aria-invalid={!!errs.account_no} />
        {errs.account_no && <span className="error">{errs.account_no}</span>}
      </label>
      <label className="field">예금주
        <input value={holder} onChange={(e) => setHolder(e.target.value)} maxLength={20} aria-invalid={!!errs.holder} />
        {errs.holder && <span className="error">{errs.holder}</span>}
      </label>
      <button className="primary" disabled={busy}>저장</button>
      {(formErr || Object.keys(errs).length > 0) && <span className="error">{formErr || `${Object.keys(errs).map((k) => FIELD[k]).join(", ")} 형식을 확인해 주세요`}</span>}
    </form>
  );
}

const Saved = ({ a }: { a: MaskedAccount }) => (
  <div className="notice ok">입력됨 · <b>{a.bank} {a.masked}</b> · 예금주 {a.holder} <span className="muted">({a.source_label} {fmtTime(a.submitted_at)})</span></div>
);

/** 담당자 화면: 환불 받을 계좌 */
export function ManagerRefundCard() {
  const { data, reload } = useLoad(() => api.get<{ due: number; paid: number; account: MaskedAccount | null; candidate: MaskedAccount | null; demo_note: string }>("/api/large/team/refund-account"));
  const { message, setMessage } = useAction();
  const [busy, setBusy] = useState(false);
  const [edit, setEdit] = useState(false);
  if (!data || (data.due === 0 && !data.paid && !data.account)) return null;
  const save = async (v: { bank: string; account_no: string; holder: string }) => {
    setBusy(true);
    try {
      await api.post("/api/large/team/refund-account", v);
      setMessage({ kind: "ok", text: "저장했습니다. 총무가 확인 후 송금합니다(뒤 4자리만 보입니다)." });
      setEdit(false);
      reload();
    } finally { setBusy(false); }
  };
  return (
    <div className="card">
      <h2>환불 받을 계좌 {data.due > 0 ? <span className="pill info">돌려받을 금액 {won(data.due)}</span> : data.paid ? <span className="pill ok">송금 완료 {won(data.paid)}</span> : null}</h2>
      <p className="muted" style={{ marginTop: 0 }}>회비는 팀 단위라 환불도 담당자님 계좌로 한 번에 보냅니다. 총무가 은행 앱에서 직접 보내고 '송금 완료'로 표시합니다(이 앱은 송금하지 않음).</p>
      <Msg m={message} />
      {data.account && !edit ? (
        <><Saved a={data.account} />{data.due > 0 && <button className="small" onClick={() => setEdit(true)}>다른 계좌로 바꾸기</button>}</>
      ) : data.due > 0 ? (
        <AccountForm onSubmit={save} busy={busy} demoNote={data.demo_note} />
      ) : null}
      {data.candidate && !data.account && <p className="muted">슬랙 답장에서 읽은 계좌({data.candidate.bank} {data.candidate.masked})는 총무 확인 전입니다.</p>}
    </div>
  );
}

interface LinkInfo { event: string; team: string; manager: string; due: number; expires_at: string; account: MaskedAccount | null; demo_note: string }

/** 환불 안내 DM 의 개인 링크(#t=토큰): 로그인 없이 그 부서 담당자만, 7일 */
export default function RefundAccountPage() {
  const [token] = useState(() => new URLSearchParams(window.location.hash.slice(1)).get("t") ?? "");
  const [info, setInfo] = useState<LinkInfo | null>(null);
  const [err, setErr] = useState("");
  const { message, setMessage } = useAction();
  const [busy, setBusy] = useState(false);
  useEventName(info?.event);
  useEffect(() => {
    if (!token) { setErr("링크에 토큰이 없습니다. DM 의 링크를 그대로 열어 주세요."); return; }
    api.post<LinkInfo>("/api/large/refund-link/open", { token }).then(setInfo).catch((e: ApiError) => setErr(e.message));
  }, [token]);
  const save = async (v: { bank: string; account_no: string; holder: string }) => {
    setBusy(true);
    try {
      const r = await api.post<{ account: MaskedAccount }>("/api/large/refund-link/submit", { token, ...v });
      setMessage({ kind: "ok", text: "저장했습니다. 총무가 확인 후 송금합니다." });
      if (info) setInfo({ ...info, account: r.account });
    } finally { setBusy(false); }
  };
  return (
    <Page narrow>
      <div className="card">
        <h2>환불 받을 계좌 입력</h2>
        {err && <p className="error">{err}</p>}
        {info && (
          <>
            <p><b>{info.team}</b> 담당자 {info.manager}님 · 돌려받을 금액 <b>{won(info.due)}</b></p>
            <p className="muted">이 링크는 담당자님 전용이며 {fmtTime(info.expires_at)}까지 쓸 수 있습니다. 저장한 계좌는 화면에 뒤 4자리만 보이고, 총무가 은행 앱에서 직접 송금합니다.</p>
            <Msg m={message} />
            {info.account && <Saved a={info.account} />}
            <AccountForm onSubmit={save} busy={busy} defaultHolder={info.manager} demoNote={info.demo_note} />
          </>
        )}
      </div>
    </Page>
  );
}
