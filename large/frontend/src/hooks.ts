import { useCallback, useEffect, useState } from "react";
import { ApiError } from "./api";

export function useLoad<T>(loader: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const reload = useCallback(() => {
    loader()
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e) => setError(e instanceof ApiError ? e : new ApiError(0, "NETWORK", String(e))));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  useEffect(reload, [reload]);
  return { data, error, reload, setData };
}

/** 버튼 동작: 실행 중 표시 + 오류 메시지. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ kind: "ok" | "bad"; text: string } | null>(null);
  const run = useCallback(async <T,>(fn: () => Promise<T>, ok?: string | ((r: T) => string)) => {
    setBusy(true);
    setMessage(null);
    try {
      const r = await fn();
      if (ok) setMessage({ kind: "ok", text: typeof ok === "function" ? ok(r) : ok });
      return r;
    } catch (e) {
      const err = e as ApiError;
      const detail = Array.isArray(err.detail) && typeof err.detail[0] === "string" ? ` — ${(err.detail as string[]).join(" / ")}` : "";
      setMessage({ kind: "bad", text: `${err.message}${detail}` });
      return undefined;
    } finally {
      setBusy(false);
    }
  }, []);
  return { busy, message, run, setMessage };
}
