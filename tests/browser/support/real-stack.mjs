#!/usr/bin/env node
/**
 * Private real stack for PG_TARGET=real / real-fault browser runs (started by playwright.config.ts as its web server).
 *
 *   1. API (services/api, uvicorn) on a private port with a unique DATABASE_GRAPH_PREFIX (pgtest_<run>_).
 *   2. Seed through the public import path: scripts/seed.sh with API_BASE_URL overridden.
 *   3. Agent B's real worker (services/worker) pointed at that API.
 *   4. The web dev server with VITE_API_TARGET pointed at that API (token injected by its proxy).
 *
 * Profiles (PG_STACK_PROFILE):
 *   normal (default) — uses the shared FalkorDB (DATABASE_PORT, container PG_FALKORDB_CONTAINER). On teardown it
 *     deletes ONLY graphs whose names start with this run's own pgtest_<run>_ prefix (PG_KEEP_GRAPHS=1 keeps them).
 *   fault — starts a PRIVATE FalkorDB container (PG_FAULT_DB_CONTAINER, port PG_FAULT_DB_PORT) so tests may
 *     `docker pause` it (T21), and runs the API with FAULTS=local_create_timeout_after_effect (T15). The container
 *     is removed on teardown. The shared FalkorDB is never paused or written by this profile.
 *
 * Stays in the foreground; SIGTERM/SIGINT stops every child, then cleans up. Logs: tests/browser/.real-stack/.
 */
import { execFileSync, spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, "../../..");
const logDir = path.resolve(here, "../.real-stack");
fs.mkdirSync(logDir, { recursive: true });
const profile = process.env.PG_STACK_PROFILE === "fault" ? "fault" : "normal";
const runId = process.env.PG_RUN_ID ?? Date.now().toString(36);
const apiPort = Number(process.env.PG_REAL_API_PORT ?? 8310);
const webPort = Number(process.env.PG_WEB_PORT ?? 5176);
const prefix = `pgtest_${runId}_`;
const sharedContainer = process.env.PG_FALKORDB_CONTAINER ?? "promisegraph-falkordb-1";
const faultContainer = process.env.PG_FAULT_DB_CONTAINER ?? `pgtest-falkordb-${runId}`;
const faultDbPort = Number(process.env.PG_FAULT_DB_PORT ?? 6391);
const apiUrl = `http://localhost:${apiPort}`;
const children = [];
let containerStarted = false;

// Guard: only a prefix of exactly this shape may ever be deleted.
if (!/^pgtest_[a-z0-9]+_$/.test(prefix)) throw new Error(`refusing unsafe graph prefix ${prefix}`);

function log(msg) {
  process.stdout.write(`[real-stack] ${msg}\n`);
}
function portFree(port) {
  return new Promise((resolve) => {
    const s = net.createServer().once("error", () => resolve(false)).once("listening", () => s.close(() => resolve(true)));
    s.listen(port, "127.0.0.1");
  });
}
function docker(...args) {
  return execFileSync("docker", args, { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();
}
function start(name, cmd, args, cwd, env) {
  const out = fs.openSync(path.join(logDir, `${name}.log`), "w");
  // Same process group as this script, so Playwright's tree kill reaches it even if we cannot clean up.
  const child = spawn(cmd, args, { cwd, env: { ...process.env, ...env }, stdio: ["ignore", out, out] });
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
async function waitFor(check, ms, what) {
  const t0 = Date.now();
  let last = "";
  while (Date.now() - t0 < ms) {
    try {
      const r = await check();
      if (r === true) return;
      last = String(r);
    } catch (e) {
      last = String(e);
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(`timed out waiting for ${what} (${last})`);
}
const httpOk = (url, ok = (s) => s === 200) => async () => {
  const r = await fetch(url);
  return ok(r.status) || `HTTP ${r.status}`;
};

/** Delete this run's graphs only (exact prefix match, re-checked per name). */
function cleanupGraphs() {
  if (profile === "fault") return; // the private container is removed instead
  if (process.env.PG_KEEP_GRAPHS === "1") {
    log(`PG_KEEP_GRAPHS=1: keeping graphs ${prefix}*`);
    return;
  }
  try {
    const names = docker("exec", sharedContainer, "redis-cli", "GRAPH.LIST").split("\n").map((s) => s.trim()).filter(Boolean);
    const mine = names.filter((n) => n.startsWith(prefix));
    for (const g of mine) {
      if (!g.startsWith(prefix) || g.startsWith("pg_")) continue;
      docker("exec", sharedContainer, "redis-cli", "GRAPH.DELETE", g);
    }
    log(`deleted ${mine.length} graph(s) with prefix ${prefix}: ${mine.join(", ") || "none"}`);
  } catch (e) {
    log(`graph cleanup failed: ${String(e).slice(0, 300)}`);
  }
}
function removeFaultContainer() {
  if (!containerStarted) return;
  try {
    try { docker("unpause", faultContainer); } catch { /* not paused */ }
    docker("rm", "-f", faultContainer);
    log(`removed private FalkorDB container ${faultContainer}`);
  } catch (e) {
    log(`container cleanup failed: ${String(e).slice(0, 300)}`);
  }
}

let stopping = false;
function stop(code = 0) {
  if (stopping) return;
  stopping = true;
  for (const { child } of [...children].reverse()) {
    try { child.kill("SIGTERM"); } catch { /* already gone */ }
  }
  // Give the API/worker a moment to release connections, then clean up synchronously and exit.
  setTimeout(() => {
    for (const { child } of children) { try { child.kill("SIGKILL"); } catch { /* gone */ } }
    cleanupGraphs();
    removeFaultContainer();
    process.exit(code);
  }, 1000);
}
process.on("SIGTERM", () => stop(0));
process.on("SIGINT", () => stop(0));

try {
  for (const p of [apiPort, webPort, ...(profile === "fault" ? [faultDbPort] : [])])
    if (!(await portFree(p))) throw new Error(`port ${p} is in use; set PG_REAL_API_PORT / PG_WEB_PORT / PG_FAULT_DB_PORT`);
  log(`profile ${profile} · API :${apiPort} · graph prefix ${prefix} · web :${webPort} · logs ${logDir}`);
  const common = { DATABASE_GRAPH_PREFIX: prefix, API_BASE_URL: apiUrl, CLOCK_MODE: "scenario", FAULTS: "" };

  if (profile === "fault") {
    const image = process.env.PG_FALKORDB_IMAGE ?? "falkordb/falkordb:v4.20.7";
    docker("run", "-d", "--name", faultContainer, "-p", `${faultDbPort}:6379`, image);
    containerStarted = true;
    await waitFor(() => docker("exec", faultContainer, "redis-cli", "PING") === "PONG" || "no PONG", 60_000, `${faultContainer} PING`);
    log(`private FalkorDB ${faultContainer} on :${faultDbPort}`);
    Object.assign(common, { DATABASE_HOST: "localhost", DATABASE_PORT: String(faultDbPort), FAULTS: "local_create_timeout_after_effect" });
  }

  start("api", "uv", ["run", "uvicorn", "promisegraph_api.app:app", "--port", String(apiPort)], path.join(repo, "services/api"), common);
  await waitFor(httpOk(`${apiUrl}/health/live`), 60_000, "API live");
  log("API live; seeding through scripts/seed.sh");
  await run("seed", "bash", [path.join(repo, "scripts/seed.sh")], repo, common);
  await waitFor(httpOk(`${apiUrl}/health/ready`), 30_000, "API ready");
  log(`seeded and ready${profile === "fault" ? " (FAULTS=local_create_timeout_after_effect)" : ""}; starting worker`);

  start("worker", "uv", ["run", "python", "-m", "promisegraph_worker"], path.join(repo, "services/worker"), common);
  start("web", "pnpm", ["exec", "vite", "--port", String(webPort), "--strictPort"], path.join(repo, "apps/web"), { VITE_API_TARGET: apiUrl, WEB_PORT: String(webPort) });
  await waitFor(httpOk(`http://localhost:${webPort}/api/v1/workspaces/ws_demo/info`), 60_000, "web proxy");
  log(`ready: web http://localhost:${webPort} -> ${apiUrl}`);
} catch (e) {
  log(`FAILED: ${e.message}`);
  stop(1);
}
