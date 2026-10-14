import { useEffect, useState, type ReactNode } from "react";
import { BrowserRouter, HashRouter, Navigate, Route, Routes } from "react-router-dom";
import { api, STATIC, type Meta } from "./api";
import { AppHeader, EventNameContext, MetaContext } from "./components/common";
import AdminLogin from "./pages/AdminLogin";
import AdminPage from "./pages/admin/AdminPage";
import DevInbox from "./pages/DevInbox";
import Home from "./pages/Home";
import InviteLanding from "./pages/InviteLanding";
import ManagerLogin from "./pages/ManagerLogin";
import ManagerPage from "./pages/ManagerPage";
import ParticipantPage from "./pages/ParticipantPage";
import RefundAccountPage from "./pages/RefundAccount";

// 정적 데모판(GitHub Pages)은 서버가 내부 경로를 index.html 로 돌려주지 않으므로 해시 주소(#/admin)를 쓴다.
const Router = ({ children }: { children: ReactNode }) => (STATIC ? <HashRouter>{children}</HashRouter> : <BrowserRouter basename="/large">{children}</BrowserRouter>);

export default function App() {
  const [meta, setMeta] = useState<Meta | null>(null);
  const [metaLoaded, setMetaLoaded] = useState(false);
  const [eventName, setEventName] = useState<string | null>(null);
  useEffect(() => {
    api.get<Meta>("/api/large/meta").then(setMeta).catch(() => setMeta(null)).finally(() => setMetaLoaded(true));
  }, []);
  // 시연 모드: 들어오면 바로 총무 대시보드(설정은 공용 초기설정 화면 '워크샵'에서 끝냄). 로그인·역할 전환 화면은 운영 모드의 첫 화면,
  // 시연에서는 /roles(상단 '역할 전환')로만 연다.
  const home = STATIC || meta?.demo_tools ? <Navigate to="/admin" replace /> : metaLoaded ? <Home /> : null;
  return (
    <MetaContext.Provider value={meta}>
      <EventNameContext.Provider value={{ name: eventName, setName: setEventName }}>
      <Router>
        <AppHeader />
        <Routes>
          <Route path="/" element={home} />
          {meta?.demo_tools && <Route path="/roles" element={<Home />} />}
          <Route path="/invite" element={<InviteLanding />} />
          <Route path="/me" element={<ParticipantPage />} />
          <Route path="/manager/login" element={<ManagerLogin />} />
          <Route path="/manager" element={<ManagerPage />} />
          <Route path="/refund-account" element={<RefundAccountPage />} />
          <Route path="/admin/login" element={<AdminLogin />} />
          <Route path="/admin" element={<AdminPage />} />
          {meta?.demo_tools && <Route path="/dev/inbox" element={<DevInbox />} />}
          <Route path="*" element={<main className="wrap"><p>페이지를 찾을 수 없습니다.</p></main>} />
        </Routes>
      </Router>
      </EventNameContext.Provider>
    </MetaContext.Provider>
  );
}
