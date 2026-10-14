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

// 정적 데모판(GitHub Pages)은 서버가 내부 경로를 index.html 로 돌려주지 않으므로 해시 주소(#/admin)를 쓴다.
const Router = ({ children }: { children: ReactNode }) => (STATIC ? <HashRouter>{children}</HashRouter> : <BrowserRouter basename="/large">{children}</BrowserRouter>);

export default function App() {
  const [meta, setMeta] = useState<Meta | null>(null);
  const [eventName, setEventName] = useState<string | null>(null);
  useEffect(() => {
    api.get<Meta>("/api/large/meta").then(setMeta).catch(() => setMeta(null));
  }, []);
  return (
    <MetaContext.Provider value={meta}>
      <EventNameContext.Provider value={{ name: eventName, setName: setEventName }}>
      <Router>
        <AppHeader />
        <Routes>
          <Route path="/" element={STATIC ? <Navigate to="/admin" replace /> : <Home />} />
          <Route path="/invite" element={<InviteLanding />} />
          <Route path="/me" element={<ParticipantPage />} />
          <Route path="/manager/login" element={<ManagerLogin />} />
          <Route path="/manager" element={<ManagerPage />} />
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
