import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { expect, test } from "@playwright/test";

/*
 * Fault profile (PG_TARGET=real-fault): private API on a PRIVATE FalkorDB container, API FAULTS=
 * local_create_timeout_after_effect, Agent B's real worker. Never touches the shared FalkorDB.
 */
test.skip(process.env.PG_TARGET !== "real-fault", "fault journeys run with PG_TARGET=real-fault");

const WS = "/api/v1/workspaces/ws_demo";
const container = process.env.PG_FAULT_DB_CONTAINER!;
const docker = (...a: string[]) => execFileSync("docker", a, { encoding: "utf8" }).trim();
let pageErrors: string[] = [];
test.beforeEach(({ page }) => {
  pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(String(e)));
});
test.afterEach(() => expect(pageErrors, "uncaught page errors").toEqual([]));

test("T15 real: create times out after the effect → no blind duplicate; outcome established or handed to the operator", async ({ page }) => {
  const stamp = Date.now().toString(36);
  const cs = (await (await page.request.get(`${WS}/customers?limit=100`)).json()).data as { id: string; name: string }[];
  const r = await page.request.post(`${WS}/requests`, {
    headers: { "Idempotency-Key": `rf-${randomUUID()}` },
    data: { customer_id: cs.find((c) => c.name.toLowerCase().includes("corvid"))!.id, subject: `Stream audit events to our SIEM ${stamp}`, body: "Our security operations team needs admin and sign-in audit events streamed to our SIEM over syslog with TLS in near real time. A CSV download is not enough for continuous monitoring.", channel: "manual", capability_ids: [], external_ref: null, auto_start: true, planner_mode: "scripted" },
  });
  expect(r.status()).toBe(201);
  const id = (await r.json()).data.request.id as string;
  const before = new Set(((await (await page.request.get(`${WS}/work-items?limit=100`)).json()).data as { id: string }[]).map((w) => w.id));

  await page.goto(`/requests/${id}`);
  await expect(page.getByTestId("proposal-kind")).toHaveText("Create a new task", { timeout: 60_000 });
  await page.getByTestId("authorize").click();
  await page.getByTestId("confirm-authorize").click();

  // Either the worker/reconciler establishes the outcome (Executed with a receipt), or the UI hands it to the operator.
  const executed = page.getByTestId("action-status").filter({ hasText: "Done" });
  const operator = page.getByTestId("reconcile-form");
  await expect(executed.or(operator)).toBeVisible({ timeout: 90_000 });
  const path = (await executed.count()) ? "established automatically" : "operator reconciliation offered";
  const sawUnknown = await page.locator('[data-testid="timeline-event"][data-type="action_updated"]').filter({ hasText: "Unclear if it went through" }).count();
  test.info().annotations.push({ type: "t15-path", description: `${path}; unknown state in timeline: ${sawUnknown > 0}` });

  // Invariant: exactly one new work item for this request — the timed-out create was not repeated.
  await expect.poll(async () => {
    const after = ((await (await page.request.get(`${WS}/work-items?limit=100`)).json()).data as { id: string }[]).filter((w) => !before.has(w.id));
    return after.length;
  }, { timeout: 30_000 }).toBe(1);
  if (path === "established automatically") await expect(page.getByTestId("receipt-connector")).toHaveText("PromiseGraph task list");
  await expect(page.getByTestId("request-status")).not.toContainText("Solved");
});

test("T21 real: database becomes unreachable mid-session → honest unavailable state, no stale data shown as live; recovers", async ({ page }) => {
  await page.goto("/requests");
  await expect(page.getByTestId("request-table")).toBeVisible();
  const reqId = (await (await page.request.get(`${WS}/requests?limit=1`)).json()).data[0].id as string;
  docker("pause", container);
  try {
    const t0 = Date.now();
    await page.reload();
    await expect(page.getByTestId("banner-unavailable")).toContainText("Database unavailable", { timeout: 45_000 });
    await expect(page.getByTestId("state-unavailable").first()).toBeVisible({ timeout: 45_000 });
    await expect(page.getByTestId("request-table")).toHaveCount(0);
    await page.goto(`/requests/${reqId}`);
    await expect(page.getByTestId("state-unavailable").first()).toBeVisible({ timeout: 45_000 });
    await expect(page.getByTestId("request-workspace")).toHaveCount(0);
    test.info().annotations.push({ type: "t21-detect-ms", description: String(Date.now() - t0) });
  } finally {
    docker("unpause", container);
  }
  await page.goto("/requests");
  await expect(page.getByTestId("request-table")).toBeVisible({ timeout: 45_000 });
  await expect(page.getByTestId("banner-unavailable")).toHaveCount(0, { timeout: 45_000 });
});
