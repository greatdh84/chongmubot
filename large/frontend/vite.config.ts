import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// LARGE 화면은 공용 서버(hub)에서 /large/ 아래로 제공된다. 빌드 결과(dist)는 hub 가 그대로 내려준다.
// 개발 서버(npm run dev)는 화면만 빠르게 고치기 위한 것이고, /api/large 는 실행 중인 공용 서버로 넘긴다.
const target = process.env.LARGE_API_TARGET ?? "http://127.0.0.1:8787";

export default defineConfig({
  base: "/large/",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api/large": { target, changeOrigin: false } },
  },
});
