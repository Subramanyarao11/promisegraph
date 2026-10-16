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
  await expect(page.getByTestId("planner-states")).toContainText("Scripted");
  await expect(page.getByTestId("settings-linear")).toBeVisible();
  if (info.connectors.linear === "unconfigured") await expect(page.getByTestId("settings-linear")).toContainText("Not configured");
  if (info.planners.model === "disabled") await expect(page.getByTestId("model-state")).toContainText("Live model unavailable");
  await expect(page.getByTestId("mode-badges")).toContainText("Scripted mode");
});

test("inbox filters and an investigation render real evidence paths as ordered text", async ({ page }) => {
  const reqs = (await (await page.request.get(`${WS}/requests?limit=100`)).json()).data as { id: string; status: string; subject: string }[];
  expect(reqs.length).toBeGreaterThan(0);
  await page.goto("/requests");
  await expect(page.getByTestId("request-table")).toBeVisible();
  await page.getByLabel("Status").selectOption("resolved");
  await expect(page).toHaveURL(/status=resolved/);
  const rows = page.getByTestId("request-table").locator("tbody tr");
  await expect(rows.first()).toContainText("Resolved");

  // Find a request whose context has at least one path, via the API.
  let target: string | null = null;
  for (const r of reqs.slice(0, 15)) {
    const ctx = await (await page.request.get(`${WS}/requests/${r.id}/context`)).json();
    if (ctx.data?.paths?.length) { target = r.id; break; }
  }
  test.skip(!target, "no request with graph paths in this workspace");
  await page.goto(`/requests/${target}`);
  const path = page.getByTestId("evidence-path").first();
  await expect(path.locator("ol.path > li").first()).toBeVisible();
  await expect(path.locator("ol.path")).toHaveAttribute("aria-label", /Graph path 1: /);
  await page.getByTestId("open-source").first().click();
  await expect(page.getByTestId("drawer-excerpt")).not.toBeEmpty();
  await expect(page.getByTestId("drawer-revision")).toContainText("rev ");
  await page.keyboard.press("Escape");
});

test("unknown request shows an explicit missing-entity state", async ({ page }) => {
  await page.goto("/requests/request_unknown_xyz");
  await expect(page.getByTestId("state-unknown-entity")).toContainText("Unknown request");
});
