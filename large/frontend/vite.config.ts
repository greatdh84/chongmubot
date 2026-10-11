import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 개발 서버는 /api 를 FastAPI(8000)로 넘긴다. 같은 출처로 보이므로 쿠키 세션이 그대로 동작한다.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: false } },
  },
});
