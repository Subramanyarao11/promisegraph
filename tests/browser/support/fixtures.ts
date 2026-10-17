import { test as base, expect, type APIRequestContext, type Page } from "@playwright/test";

/**
 * Shared test base: every test fails if the page throws an uncaught error (auto fixture), so no spec has to
 * remember its own beforeEach/afterEach pair. Helpers below talk to the API through the web dev proxy only.
 */
export const test = base.extend<{ failOnPageErrors: void }>({
  failOnPageErrors: [
    async ({ page }, use) => {
      const errors: string[] = [];
      page.on("pageerror", (e) => errors.push(String(e.stack ?? e)));
      await use();
      expect(errors, "uncaught page errors").toEqual([]);
    },
    { auto: true },
  ],
});
export { expect };

export const WS = "/api/v1/workspaces/ws_demo";

export async function customerIdByName(api: APIRequestContext, nameContains: string): Promise<string> {
  const cs = (await (await api.get(`${WS}/customers?limit=100`)).json()).data as { id: string; name: string }[];
  const c = cs.find((x) => x.name.toLowerCase().includes(nameContains.toLowerCase()));
  expect(c, `customer named like ${nameContains}`).toBeTruthy();
  return c!.id;
}

export async function expectNoHorizontalScroll(page: Page) {
  const [sw, cw] = await page.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]);
  expect(sw, "page must not scroll horizontally").toBeLessThanOrEqual((cw ?? 0) + 1);
}

/** Opens the collapsed "Technical details" disclosure (a native <details>). */
export async function openTechnicalDetails(page: Page) {
  await page.getByTestId("technical-details").getByText("Technical details", { exact: true }).click();
}

/** Role-based handles for the main interactive controls (their accessible names are part of what we test). */
export const ui = {
  newRequest: (page: Page) => page.getByRole("button", { name: "New request" }),
  approve: (page: Page) => page.getByRole("button", { name: "Approve and do it" }),
  confirmApprove: (page: Page) => page.getByRole("dialog", { name: "Approve this action?" }).getByRole("button", { name: "Approve and do it" }),
  openSource: (page: Page) => page.getByRole("button", { name: "Open source" }),
  closeRequest: (page: Page) => page.getByRole("button", { name: "Close request" }),
};
