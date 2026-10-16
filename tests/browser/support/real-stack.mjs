#!/usr/bin/env node
/**
 * Private real stack for PG_TARGET=real browser runs (started by playwright.config.ts as its web server).
 *
 *   1. API (services/api, uvicorn) on a private port, with a unique DATABASE_GRAPH_PREFIX so it uses fresh
 *      FalkorDB graphs and never touches the shared demo graphs (FalkorDB itself must be up on DATABASE_PORT).
 *   2. Seed through the public import path: scripts/seed.sh with API_BASE_URL overridden.
 *   3. Agent B's real worker (services/worker) pointed at that API.
 *   4. The web dev server with VITE_API_TARGET pointed at that API (token injected by its proxy).
 *
 * Stays in the foreground; SIGTERM/SIGINT stops every child. Writes logs to tests/browser/.real-stack/.
 * Env: PG_REAL_API_PORT (default 8310), PG_WEB_PORT (default 5176), PG_GRAPH_PREFIX (default pgtest_<time>_).
 */
import { spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, "../../..");
const logDir = path.resolve(here, "../.real-stack");
fs.mkdirSync(logDir, { recursive: true });
const apiPort = Number(process.env.PG_REAL_API_PORT ?? 8310);
const webPort = Number(process.env.PG_WEB_PORT ?? 5176);
const prefix = process.env.PG_GRAPH_PREFIX ?? `pgtest_${Date.now().toString(36)}_`;
const apiUrl = `http://localhost:${apiPort}`;
const children = [];

function log(msg) {
  process.stdout.write(`[real-stack] ${msg}\n`);
}
function portFree(port) {
  return new Promise((resolve) => {
    const s = net.createServer().once("error", () => resolve(false)).once("listening", () => s.close(() => resolve(true)));
    s.listen(port, "127.0.0.1");
  });
}
function start(name, cmd, args, cwd, env) {
  const out = fs.openSync(path.join(logDir, `${name}.log`), "w");
  const child = spawn(cmd, args, { cwd, env: { ...process.env, ...env }, stdio: ["ignore", out, out] }); // same process group: Playwright's tree kill reaches it
  children.push({ name, child });
  child.on("exit", (code, sig) => {
    if (!stopping) {
      log(`${name} exited unexpectedly (code ${code}, signal ${sig}); see ${path.join(logDir, `${name}.log`)}`);
      stop(1);
    }
  });
  return child;
}
function run(name, cmd, args, cwd, env) {
  return new Promise((resolve, reject) => {
    const out = fs.openSync(path.join(logDir, `${name}.log`), "w");
    const child = spawn(cmd, args, { cwd, env: { ...process.env, ...env }, stdio: ["ignore", out, out] });
    child.on("exit", (code) => (code === 0 ? resolve() : reject(new Error(`${name} exited ${code}; see ${path.join(logDir, `${name}.log`)}`))));
  });
}
async function waitFor(url, ms, okStatus = (s) => s < 500) {
  const t0 = Date.now();
  let last = "";
  while (Date.now() - t0 < ms) {
    try {
      const r = await fetch(url);
      if (okStatus(r.status)) return;
      last = `HTTP ${r.status}`;
    } catch (e) {
      last = String(e);
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(`timed out waiting for ${url} (${last})`);
}
let stopping = false;
function stop(code = 0) {
  if (stopping) return;
  stopping = true;
  for (const { child } of children.reverse()) {
    try { child.kill("SIGTERM"); } catch { /* already gone */ }
  }
  setTimeout(() => process.exit(code), 500);
}
process.on("SIGTERM", () => stop(0));
process.on("exit", () => { for (const { child } of children) { try { child.kill("SIGKILL"); } catch { /* gone */ } } });
process.on("SIGINT", () => stop(0));

try {
  for (const p of [apiPort, webPort]) if (!(await portFree(p))) throw new Error(`port ${p} is in use; set PG_REAL_API_PORT / PG_WEB_PORT`);
  log(`API :${apiPort} graph prefix ${prefix} · web :${webPort} · logs ${logDir}`);
  const common = { DATABASE_GRAPH_PREFIX: prefix, API_BASE_URL: apiUrl, CLOCK_MODE: "scenario", FAULTS: "" };

  start("api", "uv", ["run", "uvicorn", "promisegraph_api.app:app", "--port", String(apiPort)], path.join(repo, "services/api"), common);
  await waitFor(`${apiUrl}/health/live`, 60_000, (s) => s === 200);
  log("API live; seeding through scripts/seed.sh");
  await run("seed", "bash", [path.join(repo, "scripts/seed.sh")], repo, common);
  await waitFor(`${apiUrl}/health/ready`, 30_000, (s) => s === 200);
  log("seeded and ready; starting worker");

  start("worker", "uv", ["run", "python", "-m", "promisegraph_worker"], path.join(repo, "services/worker"), common);
  start("web", "pnpm", ["exec", "vite", "--port", String(webPort), "--strictPort"], path.join(repo, "apps/web"), { VITE_API_TARGET: apiUrl, WEB_PORT: String(webPort) });
  await waitFor(`http://localhost:${webPort}/api/v1/workspaces/ws_demo/info`, 60_000, (s) => s === 200);
  log(`ready: web http://localhost:${webPort} -> ${apiUrl}`);
} catch (e) {
  log(`FAILED: ${e.message}`);
  stop(1);
}
