import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { randomUUID } from "node:crypto";

/*
 * Real-stack journeys (PG_TARGET=real): private API + FalkorDB graphs + Agent B's real worker, no test driver.
 * Request texts come from fixtures/scenarios (test inputs only). Customers are found by name through the API.
 * Where a scenario allows more than one valid disposition, assertions accept any of them and check invariants:
 * real receipt, honest labels, drafts not sent, task creation never shown as resolution, verification required.
 */
test.skip((process.env.PG_TARGET ?? "mock") !== "real", "real-stack journeys run with PG_TARGET=real");

const WS = "/api/v1/workspaces/ws_demo";
let pageErrors: string[] = [];
test.beforeEach(({ page }) => {
  pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(String(e)));
});
test.afterEach(() => expect(pageErrors, "uncaught page errors").toEqual([]));

async function customerId(api: APIRequestContext, name: string): Promise<string> {
  const cs = (await (await api.get(`${WS}/customers?limit=100`)).json()).data as { id: string; name: string }[];
  const c = cs.find((x) => x.name.toLowerCase().includes(name));
  expect(c, `customer named like ${name}`).toBeTruthy();
  return c!.id;
}
async function intake(api: APIRequestContext, customer: string, subject: string, body: string) {
  const r = await api.post(`${WS}/requests`, {
    headers: { "Idempotency-Key": `rt-${randomUUID()}` },
    data: { customer_id: await customerId(api, customer), subject, body, channel: "manual", capability_ids: [], external_ref: null, auto_start: true, planner_mode: "scripted" },
  });
  expect(r.status()).toBe(201);
  return (await r.json()).data.request.id as string;
}
async function noHorizontalScroll(page: Page) {
  const [sw, cw] = await page.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]);
  expect(sw).toBeLessThanOrEqual(cw + 1);
}
/** Wait until the run leaves queued/running: a proposal awaiting review, waiting for input, or finished. */
async function waitForSettledRun(page: Page) {
  await expect(page.getByTestId("run-status")).not.toContainText(/Queued|Running/, { timeout: 60_000 });
}

test("T25 real: keyboard + 390px intake → evidence → reviewed action → outcome, persisted across refresh", async ({ page }) => {
  const subject = `Stream audit events to our SIEM ${Date.now()}`;
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/requests");
  await expect(page.getByTestId("mode-badges")).toContainText("Scripted mode");

  await page.getByTestId("new-request").focus();
  await page.keyboard.press("Enter");
  const dlg = page.getByTestId("new-request-dialog");
  await dlg.getByLabel("Customer").focus();
  await page.keyboard.type("Corvid");
  await expect(dlg.getByLabel("Customer")).not.toHaveValue("");
  await dlg.getByLabel("Subject").focus();
  await page.keyboard.type(subject);
  await dlg.getByLabel("Request body").focus();
  await page.keyboard.type("Our security operations team needs admin and sign-in audit events streamed to our SIEM over syslog with TLS in near real time. A CSV download is not enough for continuous monitoring.");
  await dlg.getByTestId("submit-request").focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/requests\/[^/]+$/);

  await waitForSettledRun(page);
  await expect(page.getByTestId("proposal-kind")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("finding-text")).not.toBeEmpty();
  await expect(page.getByTestId("timeline").first()).toContainText("Scripted mode");
  await noHorizontalScroll(page);

  // Evidence by keyboard (real source revision from FalkorDB).
  await page.getByTestId("open-source").first().focus();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("drawer-excerpt")).not.toBeEmpty();
  await expect(page.getByTestId("drawer-revision")).toContainText("rev ");
  await page.keyboard.press("Escape");

  // Reviewed action: authorize when the policy requires it (create/link/escalate); answer/clarify drafts auto-commit.
  const authorize = page.getByTestId("authorize");
  if (await authorize.count()) {
    await expect(authorize).toBeEnabled();
    await authorize.focus();
    await page.keyboard.press("Enter");
    await page.getByTestId("confirm-authorize").focus();
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("live-polite")).toContainText(/Authorized/);
  }
  await expect(page.getByTestId("action-status")).toContainText("Executed", { timeout: 60_000 });
  await expect(page.getByTestId("receipt-connector")).toHaveText("Local task"); // Linear is unconfigured: never labelled Linear
  await expect(page.getByTestId("request-status")).not.toContainText("Resolved");
  await expect(page.getByTestId("next-step")).toBeVisible();

  const receiptHead = (await page.getByTestId("receipt").innerText()).split("\n")[0];
  await page.reload();
  await expect(page.getByTestId("receipt")).toContainText(receiptHead);
  await expect(page.getByTestId("request-status")).not.toContainText("Resolved");

  const form = page.getByTestId("observation-form");
  await form.getByLabel("What was observed").focus();
  await page.keyboard.type("Customer confirmed audit events now arrive in their SIEM.");
  await form.getByTestId("submit-observation").focus();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("request-status")).toContainText("Resolved · verified", { timeout: 20_000 });
  await expect(page.getByTestId("resolved-evidence")).toBeVisible();
  await noHorizontalScroll(page);
});

test("real: clarification reply resumes the same run with history retained (T02/T03)", async ({ page }) => {
  const id = await intake(page.request, "tamarind", `Provisioning for store managers ${Date.now()}`, "We need provisioning for our identity provider before 200 store managers join next month. Can you turn it on?");
  await page.goto(`/requests/${id}`);
  await expect(page.getByTestId("request-status")).toContainText("Waiting for input", { timeout: 60_000 });
  await expect(page.getByTestId("draft-question")).not.toBeEmpty();
  await expect(page.getByText("Draft clarification question — not sent")).toBeVisible();
  const runHeading = await page.getByTestId("timeline-section").locator("h3").first().innerText();

  const reply = page.getByTestId("reply-form");
  await reply.getByLabel("Customer reply").fill("We mean group sync: our IdP groups 'Store Managers' and 'Regional Leads' should map to the Viewer and Editor roles. Accounts are already created at first login.");
  await reply.getByTestId("submit-reply").click();
  await expect(page.getByTestId("live-polite")).toContainText(/Reply recorded/);

  // Any valid follow-up disposition; the run is the same logical run with its earlier events.
  await expect(page.getByTestId("request-status")).not.toContainText(/Waiting for input|Investigating|Open/, { timeout: 60_000 });
  const headings = page.getByTestId("timeline-section").locator("h3");
  await expect(headings).toHaveCount(1);
  await expect(headings.first()).toHaveText(runHeading);
  await expect(page.locator('[data-testid="timeline-event"][data-type="waiting_for_input"]').first()).toBeVisible();
  await expect(page.getByTestId("proposal-kind")).not.toHaveText("Clarify (draft question)");
});

test("real: a knowledge change makes the waiting proposal stale and removes the commit control (T12)", async ({ page }) => {
  const id = await intake(page.request, "helix", `Webhook events lost again during maintenance ${Date.now()}`, "Events sent during last Sunday's maintenance window are missing again. We need them redelivered and this fixed permanently.");
  await page.goto(`/requests/${id}`);
  await expect(page.getByTestId("authorize")).toBeEnabled({ timeout: 60_000 });

  // Change a fact the proposal consulted: the linked work item (local) if there is one, else a request-scoped decision.
  const proposals = (await (await page.request.get(`${WS}/proposals?request_id=${id}`)).json()).data;
  const workId: string | undefined = proposals[0]?.payload?.work_item_id;
  let changed = false;
  if (workId) {
    const w = (await (await page.request.get(`${WS}/work-items/${workId}`)).json()).data;
    if (w.connector === "local") {
      const r = await page.request.patch(`${WS}/work-items/${workId}`, {
        headers: { "Idempotency-Key": `rt-${randomUUID()}` },
        data: { expected_version: w.version, status: w.status, owner_id: w.owner_id, forecast_at: "2026-12-01T00:00:00Z", reason: "Browser test: forecast changed before review." },
      });
      changed = r.ok();
    }
  }
  test.info().annotations.push({ type: "knowledge-change", description: changed ? `PATCH work item ${workId}` : "request-scoped decision recorded" });
  if (!changed) {
    const rev = (await (await page.request.get(`${WS}/info`)).json()).meta.knowledge_revision;
    const r = await page.request.post(`${WS}/decisions`, {
      headers: { "Idempotency-Key": `rt-${randomUUID()}` },
      data: { operation: "record", decision_id: null, rule_type: "forbidden_workaround", value: "browser_test_marker", scope: { entity_type: "request", entity_id: id }, rationale: "Browser test: change applicable policy before review.", effective_at: "2026-10-01T00:00:00Z", expires_at: null, evidence_ids: [], expected_revision: rev },
    });
    expect(r.ok(), await r.text()).toBeTruthy();
  }

  await expect(page.getByTestId("proposal-validity")).toContainText(/Stale|Invalid/, { timeout: 20_000 });
  await expect(page.getByTestId("authorize")).toBeDisabled();
  await expect(page.getByTestId("commit-blocked")).toBeVisible();
});

test("real: explicit closure is distinct from verified resolution", async ({ page }) => {
  const id = await intake(page.request, "corvid", `Stream audit events to our SIEM (closure) ${Date.now()}`, "Our security operations team needs admin and sign-in audit events streamed to our SIEM over syslog with TLS.");
  await page.goto(`/requests/${id}`);
  await waitForSettledRun(page);
  await page.getByTestId("close-request").click();
  await page.getByLabel("Reason").fill("Handled by the account team outside PromiseGraph.");
  await page.getByRole("dialog").getByRole("button", { name: "Close request" }).click();
  await expect(page.getByTestId("request-status")).toContainText("Closed", { timeout: 15_000 });
  await expect(page.getByTestId("resolved-evidence")).toHaveCount(0);
});
