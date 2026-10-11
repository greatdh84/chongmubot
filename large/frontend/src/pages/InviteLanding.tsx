import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { Page } from "../components/common";

/** 초대 링크 착지. 토큰은 URL 의 # 뒤에 있어 서버 로그에 남지 않고, 읽자마자 주소창에서 지운 뒤 세션으로 바꾼다. */
export default function InviteLanding() {
  const nav = useNavigate();
  const [error, setError] = useState("");
  const started = useRef(false);
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const token = new URLSearchParams(window.location.hash.slice(1)).get("t");
    window.history.replaceState(null, "", `${import.meta.env.BASE_URL}invite`); // 주소창·방문 기록에서 토큰 제거
    if (!token) {
      setError("초대 링크가 올바르지 않습니다.");
      return;
    }
    api
      .post("/api/large/auth/invite/exchange", { token })
      .then(() => nav("/me", { replace: true }))
      .catch((e) => setError(e.message));
  }, [nav]);
  return (
    <Page narrow>
      <div className="card">
        <h2>초대 링크 확인</h2>
        {error ? <p className="error">{error}</p> : <p className="muted">확인 중입니다…</p>}
      </div>
    </Page>
  );
}
