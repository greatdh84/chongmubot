import { useEffect, useState } from "react";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { api, type Meta } from "./api";
import { MetaContext, ModeBanner } from "./components/common";
import AdminLogin from "./pages/AdminLogin";
import AdminPage from "./pages/admin/AdminPage";
import DevInbox from "./pages/DevInbox";
import Home from "./pages/Home";
import InviteLanding from "./pages/InviteLanding";
import ManagerLogin from "./pages/ManagerLogin";
import ManagerPage from "./pages/ManagerPage";
import ParticipantPage from "./pages/ParticipantPage";

export default function App() {
  const [meta, setMeta] = useState<Meta | null>(null);
  useEffect(() => {
    api.get<Meta>("/api/large/meta").then(setMeta).catch(() => setMeta(null));
  }, []);
  return (
    <MetaContext.Provider value={meta}>
      <BrowserRouter basename="/large">
        <ModeBanner />
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/invite" element={<InviteLanding />} />
          <Route path="/me" element={<ParticipantPage />} />
          <Route path="/manager/login" element={<ManagerLogin />} />
          <Route path="/manager" element={<ManagerPage />} />
          <Route path="/admin/login" element={<AdminLogin />} />
          <Route path="/admin" element={<AdminPage />} />
          {meta?.demo_tools && <Route path="/dev/inbox" element={<DevInbox />} />}
          <Route path="*" element={<main className="wrap"><p>페이지를 찾을 수 없습니다.</p></main>} />
        </Routes>
      </BrowserRouter>
    </MetaContext.Provider>
  );
}
