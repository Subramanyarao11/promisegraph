import { expect, test } from "@playwright/test";

/*
 * Read-only checks: safe against the shared real API (PG_TARGET=real) as well as the mock.
 * No commands are sent; request IDs are discovered from the API (never hard-coded).
 */
const WS = "/api/v1/workspaces/ws_demo";
let pageErrors: string[] = [];
test.beforeEach(({ page }) => {
  pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(String(e)));
});
test.afterEach(() => expect(pageErrors).toEqual([]));

test("settings show the API's actual planner/connector states", async ({ page }) => {
  const info = (await (await page.request.get(`${WS}/info`)).json()).data;
  await page.goto("/settings");
  await expect(page.getByTestId("planner-states")).toContainText("Rule-based mode");
  await expect(page.getByTestId("settings-linear")).toBeVisible();
  if (info.connectors.linear === "unconfigured") await expect(page.getByTestId("settings-linear")).toContainText("Not set up");
  if (info.planners.model === "disabled") await expect(page.getByTestId("model-state")).toContainText("no AI model connected");
  await expect(page.getByTestId("mode-badges")).toContainText("Rule-based mode (no AI model connected)");
});

test("inbox filters and an investigation render real evidence paths as ordered text", async ({ page }) => {
  const reqs = (await (await page.request.get(`${WS}/requests?limit=100`)).json()).data as { id: string; status: string; subject: string }[];
  expect(reqs.length).toBeGreaterThan(0);
  await page.goto("/requests");
  await expect(page.getByTestId("request-table")).toBeVisible();
  await page.getByLabel("Status").selectOption("resolved");
  await expect(page).toHaveURL(/status=resolved/);
  const rows = page.getByTestId("request-table").locator("tbody tr");
  await expect(rows.first()).toContainText("Solved (confirmed)");

  // Find a request whose context has at least one path, via the API.
  let target: string | null = null;
  for (const r of reqs.slice(0, 15)) {
    const ctx = await (await page.request.get(`${WS}/requests/${r.id}/context`)).json();
    if (ctx.data?.paths?.length) { target = r.id; break; }
  }
  test.skip(!target, "no request with graph paths in this workspace");
  await page.goto(`/requests/${target}`);
  // Graph detail lives in the collapsible Technical details; the main view shows a plain one-line chain.
  await expect(page.getByTestId("chain")).toContainText("→");
  await expect(page.getByTestId("knowledge-revision")).toBeHidden();
  await page.getByTestId("technical-details").locator("summary").click();
  await expect(page.getByTestId("knowledge-revision")).toBeVisible();
  const path = page.getByTestId("evidence-path").first();
  await expect(path.locator("ol.path > li").first()).toBeVisible();
  await expect(path.locator("ol.path")).toHaveAttribute("aria-label", /Graph path 1: /);
  await page.getByTestId("open-source").first().click();
  await expect(page.getByTestId("drawer-excerpt")).not.toBeEmpty();
  await expect(page.getByTestId("drawer-revision")).toContainText("version ");
  await page.keyboard.press("Escape");
});

test("unknown request shows an explicit missing-entity state", async ({ page }) => {
  await page.goto("/requests/request_unknown_xyz");
  await expect(page.getByTestId("state-unknown-entity")).toContainText("Unknown request");
});

test("owner activity from GET /people: an inactive owner is labelled, not presented as accountable", async ({ page }) => {
  const work = (await (await page.request.get(`${WS}/work-items?limit=100`)).json()).data as { id: string; owner_id: string | null }[];
  const owners = [...new Set(work.map((w) => w.owner_id).filter((o): o is string => Boolean(o)))];
  let inactive: { id: string; name: string } | null = null;
  for (const id of owners) {
    const r = await page.request.get(`${WS}/people/${encodeURIComponent(id)}`);
    expect(r.ok(), `GET /people/${id}`).toBeTruthy();
    const p = (await r.json()).data;
    if (p.active === false) { inactive = p; break; }
  }
  test.skip(!inactive, "no work item owned by an inactive person in this workspace");
  const item = work.find((w) => w.owner_id === inactive!.id)!;
  test.info().annotations.push({ type: "inactive-owner", description: `${inactive!.id} owns ${item.id}` });

  await page.goto("/work");
  const row = page.getByTestId(`work-row-${item.id}`);
  await expect(row.getByTestId("owner")).toContainText(inactive!.name);
  await expect(row.getByTestId("owner")).toContainText("Inactive owner");

  // An active owner on another row is not flagged.
  const active = work.find((w) => w.owner_id && w.owner_id !== inactive!.id);
  if (active) {
    const p = (await (await page.request.get(`${WS}/people/${active.owner_id}`)).json()).data;
    if (p.active) await expect(page.getByTestId(`work-row-${active.id}`).getByTestId("owner")).not.toContainText("Inactive owner");
  }
});

test("first-run 'How it works' explains the 4 steps; dismissal is remembered; the nav link reopens it", async ({ page }) => {
  await page.goto("/requests");
  const how = page.getByTestId("how-it-works");
  await expect(how).toBeVisible();
  for (const step of ["A customer asks", "It checks what we know", "It suggests a next step", "You approve, then confirm"]) await expect(how).toContainText(step);
  await page.getByTestId("dismiss-how").focus();
  await page.keyboard.press("Enter");
  await expect(how).toHaveCount(0);
  await page.reload();
  await expect(page.getByTestId("request-table")).toBeVisible();
  await expect(page.getByTestId("how-it-works")).toHaveCount(0);
  await page.getByTestId("nav-how-it-works").click();
  await expect(page.getByTestId("how-it-works")).toBeVisible();
});

test("'Try an example' only prefills the form (customer found by name); nothing is submitted", async ({ page }) => {
  const before = (await (await page.request.get(`${WS}/requests?limit=100`)).json()).data.length;
  await page.goto("/requests");
  await page.getByTestId("new-request").click();
  const dlg = page.getByTestId("new-request-dialog");
  await dlg.getByRole("button", { name: "Vague request" }).click();
  await expect(dlg.getByLabel("Subject")).toHaveValue("Provisioning for store managers");
  await expect(dlg.getByLabel("Request body")).toHaveValue(/We need provisioning/);
  await expect(dlg.getByLabel("Customer").locator("option:checked")).toContainText("Tamarind");
  await expect(dlg).toContainText("PromiseGraph decides the next step itself");
  await expect(page.getByTestId("live-polite")).toContainText("Example filled in");
  await dlg.getByRole("button", { name: "Cancel" }).click();
  const after = (await (await page.request.get(`${WS}/requests?limit=100`)).json()).data.length;
  expect(after).toBe(before);
});
