import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// LARGE 화면은 공용 서버(hub)에서 /large/ 아래로 제공된다. 빌드 결과(dist)는 hub 가 그대로 내려준다.
// 개발 서버(npm run dev)는 화면만 빠르게 고치기 위한 것이고, /api/large 는 실행 중인 공용 서버로 넘긴다.
// npm run build:static(--mode static)은 GitHub Pages 정적 데모판: 상대 경로로 demo/large/ 에 빌드하고,
// API 대신 녹화한 응답(demo/large/fixtures.json, scripts/export_static_demo.py)을 쓴다. 녹화 파일을 지우지 않게 outDir 은 비우지 않는다.
const target = process.env.LARGE_API_TARGET ?? "http://127.0.0.1:8787";

export default defineConfig(({ mode }) => {
  const isStatic = mode === "static";
  return {
    base: isStatic ? "./" : "/large/",
    plugins: [react()],
    define: { "import.meta.env.VITE_STATIC": JSON.stringify(isStatic ? "1" : "") },
    build: isStatic ? { outDir: "../../demo/large", emptyOutDir: false } : {},
    server: {
      port: 5173,
      proxy: { "/api/large": { target, changeOrigin: false } },
    },
  };
});
