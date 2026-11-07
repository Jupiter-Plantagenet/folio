import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import fs from "node:fs";
const routes = [
  "overview",
  "inbox",
  "import",
  "document",
  "reconciliation",
  "decision",
  "approvals",
  "changes",
  "execution",
  "recovery",
  "records",
  "evidence",
  "activity",
  "settings",
];
async function signIn(page: any, name: string) {
  await page.goto("/");
  await page.getByLabel("Username").fill(name);
  await page
    .getByLabel("Password", { exact: true })
    .fill(`folio-${name}-local`);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.locator("#screen")).toBeVisible();
}
test("reviewer field draft is not overwritten by durable-state polling", async ({
  page,
}) => {
  await signIn(page, "reviewer");
  // Self-contained: a fresh database has no case until the sample is imported.
  await page.getByRole("navigation").getByRole("button", { name: "Import documents", exact: true }).click();
  await page.getByRole("button", { name: "Import authored sample packet", exact: true }).click();
  await page
    .getByRole("navigation")
    .getByRole("button", { name: "Document & extracted fields", exact: true })
    .click();
  await page.getByTestId("invoice-field").fill("INV-DRAFT-NOT-SAVED");
  await page.waitForTimeout(2400);
  await expect(page.getByTestId("invoice-field")).toHaveValue(
    "INV-DRAFT-NOT-SAVED",
  );
});

test("document source navigation renders and selects the cited page", async ({ page }) => {
  let injectedPage = 0;
  await page.route("**/api/v1/workspaces/*/cases/*", async (route) => {
    const response = await route.fetch();
    const data: any = await response.json();
    const source = data?.sources?.[0];
    if (response.ok() && source && Array.isArray(source.pages)) {
      injectedPage = Math.max(0, ...source.pages.map((item: any) => item.number)) + 1;
      source.pages.push({
        number: injectedPage,
        text: "UI TEST SECOND PAGE",
        words: [{ quote: "UI TEST SECOND PAGE", start: 0, end: 20, boxes: [] }],
        text_sha256: "0".repeat(64),
      });
    }
    await route.fulfill({ response, json: data });
  });
  await signIn(page, "reviewer");
  await page.getByRole("navigation").getByRole("button", { name: "Import documents", exact: true }).click();
  await page.getByRole("button", { name: "Import authored sample packet", exact: true }).click();
  await expect.poll(() => injectedPage).toBeGreaterThan(0);
  const pageButton = page.getByRole("button", { name: `Show page ${injectedPage}`, exact: true });
  await expect(pageButton).toBeVisible();
  await pageButton.click();
  await expect(pageButton).toHaveAttribute("aria-current", "page");
  await expect(page.locator(".source-text").getByRole("button", { name: "UI TEST SECOND PAGE" })).toBeVisible();
});

test("activity filter retains focus and exposes the selected filter", async ({ page }) => {
  await signIn(page, "viewer");
  await page.getByRole("navigation").getByRole("button", { name: "Activity & audit history", exact: true }).click();
  const filter = page.getByRole("button", { name: "human", exact: true });
  await filter.click();
  await expect(filter).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator("main h1")).not.toBeFocused();
  await expect(filter).toBeFocused();
});

test("overview marks cached counts with the last successful snapshot after a read failure", async ({ page }) => {
  let failReads = false;
  await page.route("**/api/v1/workspaces/*/cases", async (route) => {
    if (failReads) {
      await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "temporary read failure" }) });
      return;
    }
    await route.continue();
  });
  await signIn(page, "viewer");
  const status = page.locator("#screen").getByRole("status");
  await expect(status).toContainText("Current queue snapshot received");
  const lastSuccess = await status.locator("time").textContent();
  expect(lastSuccess).toBeTruthy();
  failReads = true;
  await expect(status).toContainText("Current counts are unavailable. Showing the last confirmed queue snapshot");
  await expect(status).toContainText(lastSuccess!);
  await expect(page.locator(".summaryline")).toContainText("Unavailable");
});
test("all fourteen real routes render at desktop tablet mobile without global overflow", async ({
  page,
}) => {
  await signIn(page, "viewer");
  const report: any[] = [];
  for (const width of [1440, 1024, 390]) {
    await page.setViewportSize({ width, height: 900 });
    for (const route of routes) {
      await page.evaluate((r) => (location.hash = r), route);
      await expect(page.locator("main")).toHaveAttribute(
        "data-current-screen",
        route,
      );
      await expect(page.locator("main h1")).toBeVisible();
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth > innerWidth,
      );
      expect(overflow, `${route} overflow at ${width}`).toBe(false);
      if (width === 1440 || width === 390) {
        const axe = await new AxeBuilder({ page })
          .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
          .analyze();
        report.push({ route, width, violations: axe.violations });
      }
    }
  }
  fs.mkdirSync(new URL("../../evidence/documents/ux/", import.meta.url), {
    recursive: true,
  });
  fs.writeFileSync(
    new URL(
      "../../evidence/documents/ux/route-accessibility.json",
      import.meta.url,
    ),
    JSON.stringify(report, null, 2),
  );
  const serious = report.flatMap((r) =>
    r.violations
      .filter((v: any) => ["serious", "critical"].includes(v.impact))
      .map((v: any) => ({
        route: r.route,
        width: r.width,
        id: v.id,
        impact: v.impact,
        nodes: v.nodes.length,
      })),
  );
  fs.writeFileSync(
    new URL(
      "../../evidence/documents/ux/serious-violations.json",
      import.meta.url,
    ),
    JSON.stringify(serious, null, 2),
  );
  expect(serious).toEqual([]);
});
