import { defineConfig, devices } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";
const __dirname = path.dirname(fileURLToPath(import.meta.url));

// PG_TARGET=mock (default): starts the coordinator's protocol mock on :8100 and the web dev server pointed at it.
// PG_TARGET=real: expects the real API on :8000 (and the real worker) already running; mock-only fault tests skip.
const target = process.env.PG_TARGET ?? "mock";
const repo = path.resolve(__dirname, "../..");
const apiUrl = target === "real" ? (process.env.PG_API_URL ?? "http://localhost:8000") : (process.env.PG_MOCK_URL ?? "http://localhost:8100");
const webPort = Number(process.env.PG_WEB_PORT ?? 5174);

export default defineConfig({
  testDir: "./specs",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  use: { baseURL: `http://localhost:${webPort}`, trace: "retain-on-failure", screenshot: "only-on-failure" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    ...(target === "mock"
      ? [{ command: "uv run uvicorn mock_api.app:app --port 8100", cwd: path.join(repo, "contracts/mock"), url: "http://localhost:8100/health/live", reuseExistingServer: true, timeout: 60_000 }]
      : []),
    {
      command: `pnpm exec vite --port ${webPort} --strictPort`,
      cwd: path.join(repo, "apps/web"),
      url: `http://localhost:${webPort}`,
      reuseExistingServer: true,
      timeout: 60_000,
      env: { VITE_API_TARGET: apiUrl, WEB_PORT: String(webPort) },
    },
  ],
});
