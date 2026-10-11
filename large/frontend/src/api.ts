// API 호출 래퍼. 쿠키 세션을 쓰고, 상태 변경 요청에는 서버가 요구하는 헤더를 붙인다.
export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public detail?: unknown) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method, credentials: "same-origin", headers: { "X-Requested-With": "chongmu" } };
  if (body instanceof FormData) init.body = body;
  else if (body !== undefined) {
    init.body = JSON.stringify(body);
    (init.headers as Record<string, string>)["Content-Type"] = "application/json";
  }
  const res = await fetch(path, init);
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) {
    const err = data?.error ?? {};
    throw new ApiError(res.status, err.code ?? "HTTP_" + res.status, err.message ?? "요청에 실패했습니다", err.detail);
  }
  return data as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body ?? {}),
  put: <T>(path: string, body: unknown) => request<T>("PUT", path, body),
  del: <T>(path: string) => request<T>("DELETE", path),
  upload: <T>(path: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<T>("POST", path, fd);
  },
};

export type Choice = "bus" | "car" | "absent" | "undecided";
export const CHOICE_LABEL: Record<string, string> = { bus: "참석+버스", car: "참석+자차", absent: "불참", undecided: "미정", none: "미응답" };

export interface Meta { app_mode: "demo" | "production"; email_mode: "mock" | "smtp"; llm_mode: "mock" | "gemini"; demo_tools: boolean }

export interface EventInfo {
  id: number; name: string; event_at: string; attend_deadline: string; payment_deadline: string;
  fees: { bus: number; car: number; absent: number }; account_note: string; snapshot_taken_at: string | null; after_attend_deadline: boolean;
}

export interface Change {
  id: number; participant_id: number; participant_name: string | null; from_choice: string | null; to_choice: string;
  from_label: string; to_label: string; after_deadline: boolean; status: string; source: string; requested_at: string;
  decided_at: string | null; liability_before: number | null; liability_after: number | null; refund_amount: number;
  extra_charge: number; rule_text: string; note: string;
}

export interface Me {
  participant: { id: number; name: string; email_masked: string; team: { id: number; code: string; name: string } };
  event: EventInfo;
  response: { choice: Choice | null; label: string; fee: number | null; snapshot_choice: string | null };
  pending_change: Change | null;
  changes: Change[];
  is_manager: boolean;
}

export interface ChatMessage { id: number; sender: "user" | "agent" | "system"; text: string; created_at: string; tools: { tool: string; ok: boolean; dry_run: boolean }[]; rules: string[] }
export interface ChatThread { id: number; role: string; state: "idle" | "awaiting_info" | "awaiting_confirmation"; pending: Record<string, unknown> | null; messages: ChatMessage[] }

export interface Task {
  id: number; kind: string; assignee: string; team_id: number | null; participant_id: number | null; transaction_id: number | null;
  change_id: number | null; status: string; blocking: boolean; question: string; options: { key: string; label: string }[];
  answer: string | null; answer_note: string; answered_at: string | null; resolution: string; evidence: Record<string, unknown>;
  rule: string; created_by: string; created_at: string;
}

export interface SettlementLine { participant_id: number; name: string; snapshot_choice: string | null; choice: string | null; choice_label: string; fee: number | null; unresolved: boolean; changes: { change_id: number; from: string | null; to: string; status: string; refund_amount: number; extra_charge: number; rule: string }[] }
export interface Receipt { allocation_id: number; transaction_id: number; txn_at: string; depositor: string; amount: number; participant_id: number | null; kind: string; status: string; basis: string }
export interface SettlementCalc {
  team_id: number; team_code: string; team_name: string; headcount: number; final_fee: number; received: number; refunds_paid: number;
  balance: number; balance_label: string; lines: SettlementLine[]; receipts: Receipt[]; blockers: string[]; rules: string[];
}

export const won = (v: number | null | undefined) => (v === null || v === undefined ? "미확정" : `${v.toLocaleString("ko-KR")}원`);
export const fmtTime = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString("ko-KR", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "-";
