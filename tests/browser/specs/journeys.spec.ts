import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { randomUUID } from "node:crypto";
import { ScriptedDriver } from "../support/driver";

const MOCK = (process.env.PG_TARGET ?? "mock") === "mock";
const WS = "/api/v1/workspaces/ws_demo";
let driver: ScriptedDriver;

let pageErrors: string[] = [];
test.beforeEach(async ({ page }) => {
  pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(String(e.stack ?? e)));
  test.skip(!MOCK, "Mock-driven journey; real-service runs use the real worker (G2/G5).");
  driver = new ScriptedDriver();
  await driver.reset();
  driver.start();
});
test.afterEach(async ({}, info) => {
  driver?.stop();
  expect(pageErrors, "uncaught page errors").toEqual([]);
  if (info.status !== info.expectedStatus) console.log(driver?.log.join("\n"));
});

/** Operator intake through the web dev proxy (token injected server-side), used to set up non-intake journeys. */
async function intake(api: APIRequestContext, subject: string, body: string) {
  const cust = await (await api.get(`${WS}/customers`)).json();
  const r = await api.post(`${WS}/requests`, {
    headers: { "Idempotency-Key": `t-${randomUUID()}` },
    data: { customer_id: cust.data[0].id, subject, body, channel: "manual", capability_ids: [], external_ref: null, auto_start: true, planner_mode: "scripted" },
  });
  expect(r.status()).toBe(201);
  return (await r.json()).data.request.id as string;
}

async function noHorizontalScroll(page: Page) {
  const [sw, cw] = await page.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]);
  expect(sw, "page must not scroll horizontally").toBeLessThanOrEqual(cw + 1);
}

test("T25 keyboard + 390px journey: intake → evidence → reviewed action → outcome; persists across refresh", async ({ page }) => {
  const subject = `Scheduled exports to SharePoint ${Date.now()}`;
  driver.plan("Scheduled exports to SharePoint", [{ kind: "create_work" }]);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/requests");

  // Honest mode labelling from /info (T22 visible part).
  await expect(page.getByTestId("mode-badges")).toContainText("Rule-based mode");
  await expect(page.getByTestId("model-state")).toContainText(/no AI model connected/);

  // Intake, keyboard only.
  await page.getByTestId("new-request").focus();
  await page.keyboard.press("Enter");
  const dlg = page.getByTestId("new-request-dialog");
  await expect(dlg).toBeVisible();
  await dlg.getByLabel("Customer").focus();
  const firstCustomer = (await (await page.request.get(`${WS}/customers`)).json()).data[0].name as string;
  await page.keyboard.type(firstCustomer.slice(0, 3)); // type-to-select on a focused <select>
  await expect(dlg.getByLabel("Customer")).not.toHaveValue("");
  await dlg.getByLabel("Subject").focus();
  await page.keyboard.type(subject);
  await dlg.getByLabel("Request body").focus();
  await page.keyboard.type("We need scheduled exports delivered to a SharePoint folder every Monday.");
  await expect(dlg.getByLabel("Rule-based mode")).toBeChecked();
  await dlg.getByTestId("submit-request").focus();
  await page.keyboard.press("Enter");

  await expect(page).toHaveURL(/\/requests\/request_/);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(subject);
  await noHorizontalScroll(page);

  // Evidence: proposal with finding + alternatives, sources, path text.
  await expect(page.getByTestId("proposal-kind")).toHaveText("Create a new task", { timeout: 20_000 });
  await expect(page.getByTestId("request-status")).toContainText("Waiting for your approval");
  await expect(page.getByTestId("run-status")).toContainText("Paused: waiting for your approval");
  await expect(page.getByTestId("finding-text")).toContainText("create_work");
  await expect(page.getByTestId("alternatives")).toContainText("Attach to existing work");
  await expect(page.getByTestId("acceptance-criteria")).toContainText("Customer can complete the workflow");
  await expect(page.getByTestId("proposal-owner")).toContainText("No owner yet");
  await expect(page.getByTestId("execution-target")).toContainText("PromiseGraph's task list");
  await expect(page.getByTestId("timeline").first()).toContainText("Rule-based mode");

  // Source drawer by keyboard: exact excerpt + revision + freshness; Escape closes.
  const openSource = page.getByTestId("open-source").first();
  await openSource.focus();
  await page.keyboard.press("Enter");
  const drawer = page.getByTestId("source-drawer");
  await expect(drawer.getByTestId("drawer-excerpt")).not.toBeEmpty();
  await expect(drawer.getByTestId("drawer-revision")).toContainText("version ");
  await expect(drawer.getByTestId("drawer-freshness")).toContainText(/Up to date|May be out of date/);
  await page.keyboard.press("Escape");
  await expect(drawer).not.toBeVisible();

  // Reviewed action: authorize via dialog, keyboard only.
  await page.getByTestId("authorize").focus();
  await page.keyboard.press("Enter");
  await page.getByTestId("confirm-authorize").focus();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("live-polite")).toContainText(/Approved/);
  await expect(page.getByTestId("action-status")).toContainText("Done", { timeout: 20_000 });
  await expect(page.getByTestId("receipt-connector")).toHaveText("PromiseGraph task list");
  await expect(page.getByTestId("request-status")).toContainText("Waiting on engineering");
  await expect(page.getByTestId("next-step")).toContainText(/doesn.t mean the customer.s problem is solved/);
  await noHorizontalScroll(page);

  // Persisted refresh: everything is re-read from the API.
  const receiptText = await page.getByTestId("receipt").innerText();
  await page.reload();
  await expect(page.getByTestId("receipt")).toContainText(receiptText.split("\n")[0]);
  await expect(page.getByTestId("receipt-connector")).toHaveText("PromiseGraph task list");
  await expect(page.getByTestId("request-status")).toContainText("Waiting on engineering");

  // Outcome verification by keyboard.
  const form = page.getByTestId("observation-form");
  await form.getByLabel("How do you know?").focus();
  await form.getByLabel("What happened").focus();
  await page.keyboard.type("Customer confirmed the Monday export arrived in SharePoint.");
  await form.getByTestId("submit-observation").focus();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("request-status")).toContainText("Solved (confirmed)", { timeout: 15_000 });
  await expect(page.getByTestId("resolved-evidence")).toBeVisible();
  await noHorizontalScroll(page);
});

test("clarification continuation resumes the same run with history retained", async ({ page }) => {
  driver.plan("We need provisioning", [{ kind: "clarify" }, { kind: "answer" }]);
  const id = await intake(page.request, `We need provisioning ${Date.now()}`, "We need provisioning for our team.");
  await page.goto(`/requests/${id}`);
  await expect(page.getByTestId("request-status")).toContainText("Waiting for customer reply", { timeout: 20_000 });
  await expect(page.getByTestId("run-status")).toContainText("Paused: waiting for customer reply");
  await expect(page.getByTestId("draft-question")).toContainText("account creation or group synchronization");
  await expect(page.getByText("Draft question for the customer: not sent")).toBeVisible();
  const runHeading = await page.getByTestId("run-heading").first().textContent();

  const reply = page.getByTestId("reply-form");
  await reply.getByLabel("Customer reply").fill("We mean SCIM group synchronization.");
  await reply.getByTestId("submit-reply").click();
  await expect(page.getByTestId("live-polite")).toContainText(/Reply recorded/);

  await expect(page.getByTestId("proposal-kind")).toHaveText("Reply with an answer", { timeout: 20_000 });
  await expect(page.getByTestId("request-status")).toContainText("Waiting for you to confirm it's solved", { timeout: 20_000 });
  // Same logical run: one run heading, and it still holds the earlier waiting_for_input event.
  const headings = page.getByTestId("run-heading");
  await expect(headings).toHaveCount(1);
  await expect(headings.first()).toHaveText(runHeading!);
  await expect(page.locator('[data-testid="timeline-event"][data-type="waiting_for_input"]')).toHaveCount(1);
  await expect(page.getByTestId("draft-text")).toBeVisible();
});

test("stale proposal has no enabled commit control", async ({ page }) => {
  driver.plan("Stale plan check", [{ kind: "create_work", stale: true }]);
  const id = await intake(page.request, `Stale plan check ${Date.now()}`, "Need a new export destination for audit evidence.");
  await page.goto(`/requests/${id}`);
  await expect(page.getByTestId("proposal-validity")).toContainText("Out of date", { timeout: 20_000 });
  await expect(page.getByTestId("commit-blocked")).toContainText("Out of date: facts changed since this was suggested");
  await expect(page.getByTestId("authorize")).toBeDisabled();
  await expect(page.getByTestId("validation-reasons")).toContainText("based_on_revision");
});

test("dispatch timeout after effect shows outcome unknown, not a new create", async ({ page }) => {
  driver.plan("Unknown write check", [{ kind: "create_work", dispatchTimeout: true }]);
  const id = await intake(page.request, `Unknown write check ${Date.now()}`, "Need a new export destination for audit evidence.");
  await page.goto(`/requests/${id}`);
  await expect(page.getByTestId("authorize")).toBeEnabled({ timeout: 20_000 });
  await page.getByTestId("authorize").click();
  await page.getByTestId("confirm-authorize").click();
  await expect(page.getByTestId("action-status")).toContainText("Unclear if it went through", { timeout: 20_000 });
  await expect(page.getByTestId("dispatch-unknown")).toContainText("nothing is created again automatically");
  await expect(page.getByTestId("authorize")).toHaveCount(0); // no second create offered

  // Operator reconciliation (Rev 3 #20/#26). A target that does not exist is refused by the server…
  await expect(page.getByTestId("run-status")).toContainText("Paused: waiting for your approval", { timeout: 15_000 }); // auto-check gave up
  const rec = page.getByTestId("reconcile-form");
  await rec.getByLabel("Yes, I found it").check();
  await rec.getByLabel(/Its ID in/).fill("work_that_does_not_exist");
  await rec.getByRole("button", { name: "Save what I found" }).click();
  await expect(rec.getByTestId("state-error")).toContainText("Not allowed by company rules");
  await expect(page.getByTestId("action-status")).toContainText("Unclear if it went through");
  // …while the task the timed-out effect really created (found on /work) reconciles the action.
  const work = await (await page.request.get(`${WS}/work-items?limit=100`)).json();
  const created = work.data.find((w: { title: string }) => w.title.includes("Unknown write check"));
  expect(created, "the local effect was applied before the timeout").toBeTruthy();
  await rec.getByLabel(/Its ID in/).fill(created.id);
  await rec.getByRole("button", { name: "Save what I found" }).click();
  await expect(page.getByTestId("action-status")).toContainText("Done", { timeout: 15_000 });
  await expect(page.getByTestId("receipt")).toContainText(created.id);
});

test("explicit closure is distinct from verified resolution", async ({ page }) => {
  driver.plan("Closure check", [{ kind: "escalate" }]);
  const id = await intake(page.request, `Closure check ${Date.now()}`, "Please close this out; handled elsewhere.");
  await page.goto(`/requests/${id}`);
  await expect(page.getByTestId("request-status")).toContainText("Waiting for your approval", { timeout: 20_000 });
  await page.getByTestId("close-request").click();
  await page.getByLabel("Reason").fill("Handled by account team outside PromiseGraph.");
  await page.getByRole("dialog").getByRole("button", { name: "Close request" }).click();
  await expect(page.getByTestId("request-status")).toContainText("Closed", { timeout: 10_000 });
  await expect(page.getByTestId("resolved-evidence")).toHaveCount(0);
});

test("database unavailable shows an honest unavailable state, no data", async ({ page }) => {
  await page.goto("/requests");
  await expect(page.getByTestId("request-table")).toBeVisible();
  await driver.fault({ database_unavailable: true });
  try {
    await page.reload();
    await expect(page.getByTestId("banner-unavailable")).toContainText("Database unavailable", { timeout: 15_000 });
    await expect(page.getByTestId("state-unavailable")).toBeVisible();
    await expect(page.getByTestId("request-table")).toHaveCount(0);
    await page.goto("/requests/request_does_not_matter");
    await expect(page.getByTestId("state-unavailable")).toBeVisible();
  } finally {
    await driver.fault({ database_unavailable: false });
  }
  await page.goto("/requests");
  await expect(page.getByTestId("request-table")).toBeVisible();
  await expect(page.getByTestId("banner-unavailable")).toHaveCount(0, { timeout: 15_000 });
});
