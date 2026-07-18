/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

// The dev proxy is not a convenience — it is what makes dev match production.
// `CORS_ALLOW_ORIGINS` defaults to `[]`, so backend/app/main.py never installs CORSMiddleware and a
// direct cross-origin call from :5173 would fail outright. More importantly the anonymous session
// cookie is SameSite=Lax (backend/app/api/sessions.py), so a split origin silently downgrades
// anonymous chat to single-turn. Proxying keeps one origin in dev exactly as Vercel rewrites do in
// production.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const target = env.VITE_API_BASE_URL || "http://localhost:8000";
  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        "/api": { target, changeOrigin: false },
        "/internal": { target, changeOrigin: false },
      },
    },
    test: {
      globals: true,
      environment: "jsdom",
      setupFiles: ["./src/test/setup.ts"],
      css: false,
    },
  };
});
