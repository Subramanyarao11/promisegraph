import type { APIRequestContext, Page } from "@playwright/test";
import { customerIdByName, expect, expectNoHorizontalScroll, openTechnicalDetails, test, ui, WS } from "../support/fixtures";
import { randomUUID } from "node:crypto";

/*
 * Real-stack journeys (PG_TARGET=real): private API + FalkorDB graphs + Agent B's real worker, no test driver.
 * Request texts come from fixtures/scenarios (test inputs only). Customers are found by name through the API.
 * Where a scenario allows more than one valid disposition, assertions accept any of them and check invariants:
 * real receipt, honest labels, drafts not sent, task creation never shown as resolution, verification required.
 */
test.skip((process.env.PG_TARGET ?? "mock") !== "real", "real-stack journeys run with PG_TARGET=real");


async function intake(api: APIRequestContext, customer: string, subject: string, body: string) {
  const r = await api.post(`${WS}/requests`, {
    headers: { "Idempotency-Key": `rt-${randomUUID()}` },
    data: { customer_id: await customerIdByName(api, customer), subject, body, channel: "manual", capability_ids: [], external_ref: null, auto_start: true, planner_mode: "scripted" },
  });
  expect(r.status()).toBe(201);
  return (await r.json()).data.request.id as string;
}
/** Wait until the run leaves queued/running: a proposal awaiting review, waiting for input, or finished. */
async function waitForSettledRun(page: Page) {
  await expect(page.getByTestId("run-status")).not.toContainText(/Queued|Running/, { timeout: 60_000 });
}

test("T25 real: keyboard + 390px intake → evidence → reviewed action → outcome, persisted across refresh", async ({ page }) => {
  const subject = `Stream audit events to our SIEM ${Date.now()}`;
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/requests");
  await expect(page.getByTestId("mode-badges")).toContainText("Rule-based mode");

  await page.getByRole("button", { name: "New request" }).focus();
  await page.keyboard.press("Enter");
  const dlg = page.getByRole("dialog", { name: "New customer request" });
  await dlg.getByLabel("Customer").focus();
  await page.keyboard.type("Corvid");
  await expect(dlg.getByLabel("Customer")).not.toHaveValue("");
  await dlg.getByLabel("Subject").focus();
  await page.keyboard.type(subject);
  await dlg.getByLabel("Request body").focus();
  await page.keyboard.type("Our security operations team needs admin and sign-in audit events streamed to our SIEM over syslog with TLS in near real time. A CSV download is not enough for continuous monitoring.");
  await dlg.getByRole("button", { name: "Submit and look into it" }).focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/requests\/[^/]+$/);

  await waitForSettledRun(page);
  await expect(page.getByTestId("proposal-kind")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("finding-text")).not.toBeEmpty();
  await expect(page.getByTestId("timeline").first()).toContainText("Rule-based mode");
  await expectNoHorizontalScroll(page);

  // Evidence by keyboard (real source revision from FalkorDB).
  if ((await ui.openSource(page).count()) === 0) {
    await page.getByTestId("toggle-sources").focus();
    await page.keyboard.press("Enter");
  }
  await ui.openSource(page).first().focus();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("drawer-excerpt")).not.toBeEmpty();
  await expect(page.getByTestId("drawer-revision")).toContainText("version ");
  await page.keyboard.press("Escape");

  // Reviewed action: authorize when the policy requires it (create/link/escalate); answer/clarify drafts auto-commit.
  const authorize = ui.approve(page);
  if (await authorize.count()) {
    await expect(authorize).toBeEnabled();
    await authorize.focus();
    await page.keyboard.press("Enter");
    await ui.confirmApprove(page).focus();
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("live-polite")).toContainText(/Approved/);
  }
  await expect(page.getByTestId("action-status")).toContainText("Done", { timeout: 60_000 });
  await expect(page.getByTestId("receipt-connector")).toHaveText("PromiseGraph task list"); // Linear is unconfigured: never labelled Linear
  await expect(page.getByTestId("request-status")).not.toContainText("Solved");
  await expect(page.getByTestId("next-step")).toBeVisible();

  const receiptHead = (await page.getByTestId("receipt").innerText()).split("\n")[0];
  await page.reload();
  await expect(page.getByTestId("receipt")).toContainText(receiptHead);
  await expect(page.getByTestId("request-status")).not.toContainText("Solved");

  const form = page.getByTestId("observation-form");
  await form.getByLabel("What happened").focus();
  await page.keyboard.type("Customer confirmed audit events now arrive in their SIEM.");
  await form.getByRole("button", { name: "Save", exact: true }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("request-status")).toContainText("Solved (confirmed)", { timeout: 20_000 });
  await expect(page.getByTestId("resolved-evidence")).toBeVisible();
  await expectNoHorizontalScroll(page);
});

test("real: clarification reply resumes the same run with history retained (T02/T03)", async ({ page }) => {
  const id = await intake(page.request, "tamarind", `Provisioning for store managers ${Date.now()}`, "We need provisioning for our identity provider before 200 store managers join next month. Can you turn it on?");
  await page.goto(`/requests/${id}`);
  await expect(page.getByTestId("request-status")).toContainText("Waiting for customer reply", { timeout: 60_000 });
  await expect(page.getByTestId("draft-question")).not.toBeEmpty();
  await expect(page.getByText("Draft question for the customer: not sent")).toBeVisible();
  const runHeading = await page.getByTestId("run-heading").first().textContent();

  const reply = page.getByTestId("reply-form");
  await reply.getByLabel("Customer reply").fill("We mean group sync: our IdP groups 'Store Managers' and 'Regional Leads' should map to the Viewer and Editor roles. Accounts are already created at first login.");
  await reply.getByRole("button", { name: "Save reply and continue" }).click();
  await expect(page.getByTestId("live-polite")).toContainText(/Reply recorded/);

  // Any valid follow-up disposition; the run is the same logical run with its earlier events.
  await expect(page.getByTestId("request-status")).not.toContainText(/Waiting for customer reply|Being looked into|^New$/, { timeout: 60_000 });
  const headings = page.getByTestId("run-heading");
  await expect(headings).toHaveCount(1);
  await expect(headings.first()).toHaveText(runHeading!);
  await openTechnicalDetails(page);
  await expect(page.locator('[data-testid="timeline-event"][data-type="waiting_for_input"]').first()).toBeVisible();
  await expect(page.getByTestId("proposal-kind")).not.toHaveText("Ask the customer a question");
});

test("real: a knowledge change makes the waiting proposal stale and removes the commit control (T12)", async ({ page }) => {
  const id = await intake(page.request, "helix", `Webhook events lost again during maintenance ${Date.now()}`, "Events sent during last Sunday's maintenance window are missing again. We need them redelivered and this fixed permanently.");
  await page.goto(`/requests/${id}`);
  await expect(ui.approve(page)).toBeEnabled({ timeout: 60_000 });

  // Change a fact the proposal consulted (T12): the linked local work item. The scenario's disposition is link_work;
  // if the planner chose something else the precondition is not met and the test says exactly what it saw.
  type P = { id: string; kind: string; validity: string; payload: { work_item_id?: string } };
  let proposal: P | undefined;
  await expect.poll(async () => {
    const list = (await (await page.request.get(`${WS}/proposals?request_id=${id}`)).json()).data as P[];
    proposal = list.find((p) => p.validity === "valid");
    return proposal?.id ?? null;
  }, { timeout: 15_000 }).not.toBeNull();
  test.info().annotations.push({ type: "proposal", description: JSON.stringify(proposal).slice(0, 300) });
  expect(proposal!.kind, `precondition: scenario t12 expects link_work, planner proposed ${proposal!.kind}`).toBe("link_work");
  const workId = proposal!.payload.work_item_id!;
  const w = (await (await page.request.get(`${WS}/work-items/${workId}`)).json()).data;
  expect(w.connector, "precondition: linked work is local (Rev 3 #23)").toBe("local");
  const r = await page.request.patch(`${WS}/work-items/${workId}`, {
    headers: { "Idempotency-Key": `rt-${randomUUID()}` },
    data: { expected_version: w.version, status: w.status, owner_id: w.owner_id, forecast_at: "2026-12-01T00:00:00Z", reason: "Browser test: forecast changed before review." },
  });
  expect(r.status(), await r.text()).toBe(200);

  await expect(page.getByTestId("proposal-validity")).toContainText(/Out of date|Can't be done/, { timeout: 20_000 });
  await expect(ui.approve(page)).toBeDisabled();
  await expect(page.getByTestId("commit-blocked")).toBeVisible();
});

test("real: explicit closure is distinct from verified resolution", async ({ page }) => {
  const id = await intake(page.request, "corvid", `Stream audit events to our SIEM (closure) ${Date.now()}`, "Our security operations team needs admin and sign-in audit events streamed to our SIEM over syslog with TLS.");
  await page.goto(`/requests/${id}`);
  await waitForSettledRun(page);
  await ui.closeRequest(page).click();
  await page.getByLabel("Reason").fill("Handled by the account team outside PromiseGraph.");
  await page.getByRole("dialog").getByRole("button", { name: "Close request" }).click();
  await expect(page.getByTestId("request-status")).toContainText("Closed", { timeout: 15_000 });
  await expect(page.getByTestId("resolved-evidence")).toHaveCount(0);
});
