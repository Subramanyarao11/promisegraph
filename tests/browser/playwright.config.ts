import { defineConfig, devices } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";
const __dirname = path.dirname(fileURLToPath(import.meta.url));

/*
 * PG_TARGET=mock (default): starts a PRIVATE protocol mock (default :8199) and a private web dev server (:5174).
 *   Never reuses an already-running server: the shared :8100 mock is restarted/reset by other agents and a worker
 *   pointed at it would claim this suite's jobs — that sharing was the root cause of earlier intermittent failures.
 * PG_TARGET=real: support/real-stack.mjs starts a private API (unique DATABASE_GRAPH_PREFIX, seeded via
 *   scripts/seed.sh), Agent B's real worker and the web dev server (:5176). Requires FalkorDB on DATABASE_PORT.
 */
const target = process.env.PG_TARGET ?? "mock";
const repo = path.resolve(__dirname, "../..");
const mockPort = Number(process.env.PG_MOCK_PORT ?? 8199);
const webPort = Number(process.env.PG_WEB_PORT ?? (target === "real" ? 5176 : 5174));
process.env.PG_MOCK_URL = `http://localhost:${mockPort}`; // read by support/driver.ts in worker processes

const mockServers = [
  { command: `uv run uvicorn mock_api.app:app --port ${mockPort}`, cwd: path.join(repo, "contracts/mock"), url: `http://localhost:${mockPort}/health/live`, reuseExistingServer: false, timeout: 60_000 },
  {
    command: `pnpm exec vite --port ${webPort} --strictPort`,
    cwd: path.join(repo, "apps/web"),
    url: `http://localhost:${webPort}`,
    reuseExistingServer: false,
    timeout: 60_000,
    env: { VITE_API_TARGET: `http://localhost:${mockPort}`, WEB_PORT: String(webPort) },
  },
];
const realServers = [
  {
    command: "node support/real-stack.mjs",
    cwd: __dirname,
    url: `http://localhost:${webPort}/api/v1/workspaces/ws_demo/info`,
    reuseExistingServer: false,
    timeout: 240_000,
    stdout: "pipe" as const,
    env: { PG_WEB_PORT: String(webPort) },
  },
];

export default defineConfig({
  testDir: "./specs",
  timeout: target === "real" ? 120_000 : 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0, // deliberately none: failures must be diagnosed, not retried away
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  use: { baseURL: `http://localhost:${webPort}`, trace: "retain-on-failure", screenshot: "only-on-failure" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: target === "real" ? realServers : mockServers,
});
