// GitHub Pages 정적 데모판(npm run build:static) 전용. 서버 대신 미리 녹화한 응답(fixtures.json)으로 api.ts 요청을 처리한다.
// 녹화: large/backend/scripts/export_static_demo.py — 같은 시드·같은 코드가 계산한 응답이라 금액은 서버와 같다.
// 조회(GET)만 되고 저장은 안 된다. 총무 Agent 는 녹화해 둔 질문에만 답한다.
import { ApiError, type ChatMessage, type ChatThread } from "./api";

interface Recorded { messages: ChatMessage[]; state: ChatThread["state"]; pending: ChatThread["pending"] }
interface Fixtures {
  event_base: string;
  get: Record<string, unknown>;
  chat: { questions: string[]; answers: Record<string, Recorded>; confirms: Record<string, { ask: Recorded; yes: Recorded; no: Recorded }> };
}

const READ_ONLY = "정적 데모라 저장되지 않아요. 실제로 바꾸려면 공용 서버(python -m hub)에서 열어 주세요.";
const YES = /^(네|넵|예|응|그래|좋아|보내)/;
const NO = /^(아니|아뇨|취소|안\s?보내|하지\s?마)/;

let loading: Promise<Fixtures> | null = null;
const load = () =>
  (loading ??= fetch(new URL("fixtures.json", document.baseURI)).then((r) => {
    if (!r.ok) throw new ApiError(r.status, "NO_FIXTURES", "정적 데모 데이터(fixtures.json)를 불러오지 못했습니다");
    return r.json() as Promise<Fixtures>;
  }));

/** export_static_demo.py 의 key() 와 같은 규칙: 빈 쿼리 값 제거·이름순 정렬 */
export function fixtureKey(path: string): string {
  const [p, qs = ""] = path.split("?");
  const items = [...new URLSearchParams(qs)].filter(([, v]) => v !== "").sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return items.length ? `${p}?${items.map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&")}` : p;
}

/** 질문 비교용: 공백·끝 문장부호를 무시 */
const squash = (s: string) => s.replace(/\s+/g, "").replace(/[?.!~]+$/, "");

interface PRow { name: string; slack_id: string; employee_no: string; team: { id: number } }

/** 참가자 검색은 전체 명단을 브라우저에서 거른다(서버와 같은 조건: 이름·Slack ID·사번 포함) */
function participants(fx: Fixtures, path: string): PRow[] {
  const all = (fx.get[`${fx.event_base}/participants`] ?? []) as PRow[];
  const qs = new URLSearchParams(path.split("?")[1] ?? "");
  const team = Number(qs.get("team_id") || 0);
  const q = qs.get("q") ?? "";
  return all.filter((p) => (!team || p.team.id === team) && (!q || p.name.includes(q) || p.slack_id.includes(q.toUpperCase()) || p.employee_no.includes(q)));
}

// 총무 Agent 대화: 이 탭 안에서만 이어진다(새로고침하면 처음부터)
let thread: ChatThread | null = null;
let confirming: string | null = null;
let nextId = 1;

function append(rec: Recorded, typed?: string) {
  const t = thread!;
  rec.messages.forEach((m, i) => t.messages.push({ ...structuredClone(m), id: nextId++, ...(i === 0 && m.sender === "user" && typed ? { text: typed } : {}) }));
  t.state = rec.state;
  t.pending = rec.pending;
}

function chat(fx: Fixtures, text: string) {
  const t = thread!;
  const q = squash(text);
  const find = <T,>(m: Record<string, T>) => Object.entries(m).find(([k]) => squash(k) === q)?.[0];
  if (confirming) {
    const c = fx.chat.confirms[confirming];
    confirming = null;
    if (YES.test(q)) return append(c.yes, text);
    if (NO.test(q)) return append(c.no, text);
    t.state = "idle"; // 서버와 같이: 확인 대기 중 다른 질문이 오면 이전 확인은 취소
    t.pending = null;
  }
  const ck = find(fx.chat.confirms);
  if (ck) {
    confirming = ck;
    return append(fx.chat.confirms[ck].ask, text);
  }
  const ak = find(fx.chat.answers);
  if (ak) return append(fx.chat.answers[ak], text);
  const now = new Date().toISOString();
  const base = { created_at: now, tools: [], rules: [], llm: null };
  t.messages.push({ ...base, id: nextId++, sender: "user", text });
  t.messages.push({
    ...base, id: nextId++, sender: "agent",
    text: `정적 데모라 미리 녹화한 질문에만 답해요. 이런 질문을 해 보세요:\n${fx.chat.questions.map((x) => `- ${x}`).join("\n")}\n(실제 Agent 는 공용 서버에서 열면 어떤 질문이든 보고서를 조회해 답합니다)`,
  });
  t.state = "idle";
  t.pending = null;
}

export async function staticRequest<T>(method: string, path: string, body?: unknown): Promise<T> {
  const fx = await load();
  const p = path.split("?")[0];
  const chatPath = `${fx.event_base}/agent/chat`;
  if (p === chatPath) {
    if (!thread) {
      thread = structuredClone(fx.get[chatPath]) as ChatThread;
      nextId = Math.max(0, ...thread.messages.map((m) => m.id)) + 1;
    }
    if (method === "POST") {
      const text = String((body as { text?: string })?.text ?? "").trim();
      if (!text) throw new ApiError(422, "EMPTY", "질문을 입력하세요");
      chat(fx, text);
      return structuredClone({ thread, reply: thread.messages[thread.messages.length - 1] }) as T;
    }
    return structuredClone(thread) as T;
  }
  if (method !== "GET") throw new ApiError(409, "STATIC_DEMO", READ_ONLY);
  if (p === `${fx.event_base}/participants`) return participants(fx, path) as T;
  const key = fixtureKey(path);
  if (!(key in fx.get)) throw new ApiError(404, "NOT_RECORDED", "정적 데모에 녹화되지 않은 화면이에요. 공용 서버에서 열어 주세요.");
  return structuredClone(fx.get[key]) as T;
}
