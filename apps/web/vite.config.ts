import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// The operator token lives in the repo-root .env and is injected server-side by this dev/preview
// proxy. It is never exposed through import.meta.env (no VITE_ prefix, and envDir stays apps/web).
const repoRoot = path.resolve(__dirname, "../..");

export default defineConfig(({ mode }) => {
  const rootEnv = loadEnv(mode, repoRoot, "");
  const target = process.env.VITE_API_TARGET ?? rootEnv.VITE_API_TARGET ?? "http://localhost:8000";
  const token = process.env.OPERATOR_TOKEN ?? rootEnv.OPERATOR_TOKEN ?? "";
  const proxy = {
    "/api": {
      target,
      changeOrigin: true,
      // Replaces any client-supplied Authorization header with the server-held operator token.
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    },
    "/health": { target, changeOrigin: true },
  };
  return {
    plugins: [react()],
    resolve: { alias: { "@contracts": path.resolve(repoRoot, "contracts/generated/ts/contracts.ts") } },
    define: { __API_TARGET__: JSON.stringify(target) },
    server: { port: Number(process.env.WEB_PORT ?? 5173), strictPort: true, proxy, fs: { allow: [repoRoot] } },
    preview: { port: Number(process.env.WEB_PORT ?? 4173), proxy },
  };
});
