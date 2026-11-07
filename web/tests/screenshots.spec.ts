import { test, expect } from "@playwright/test";

// Opt-in README capture: FOLIO_SCREENSHOTS=1. Builds real state first (import,
// decision, separate approval, verified execution), then photographs each screen.
test.skip(process.env.FOLIO_SCREENSHOTS !== "1", "README screenshots are opt-in");

const out = "../docs/screenshots/";

async function signIn(page: any, name: string) {
  await page.goto("/");
  await page.getByLabel("Username").fill(name);
  await page.getByLabel("Password", { exact: true }).fill(`folio-${name}-local`);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.locator("#screen")).toBeVisible();
}

async function signOut(page: any) {
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Sign in to Folio" })).toBeVisible();
}

async function shoot(page: any, route: string, file = route) {
  await page.evaluate((r: string) => (location.hash = r), route);
  await expect(page.locator("main")).toHaveAttribute("data-current-screen", route);
  await expect(page.locator("main h1")).toBeVisible();
  await page.waitForTimeout(400);
  await page.screenshot({ path: `${out}${file}.png`, fullPage: false });
}

test("capture all fourteen screens with real state", async ({ page }) => {
  test.setTimeout(120_000);
  await page.setViewportSize({ width: 1440, height: 900 });
  const nav = page.getByRole("navigation", { name: "Workspace navigation" });

  await signIn(page, "reviewer");
  await nav.getByRole("button", { name: "Import documents", exact: true }).click();
  await shoot(page, "import");
  await page.getByRole("button", { name: "Import authored sample packet" }).click();
  await page.locator(".source-text .source-word").nth(4).click();
  await shoot(page, "document");
  await page.getByRole("button", { name: "Compare documents", exact: true }).click();
  await shoot(page, "reconciliation");
  await page.getByRole("button", { name: "Resolve exception", exact: true }).click();
  await page.getByLabel("Decision reason").fill("Only 100 units confirmed by goods receipt GR-208");
  await shoot(page, "decision");
  await page.getByTestId("save-decision").click();
  await expect(page.getByRole("heading", { name: "Review proposed changes" })).toBeVisible();
  await shoot(page, "changes");
  await signOut(page);

  await signIn(page, "approver");
  await nav.getByRole("button", { name: "Approval queue", exact: true }).click();
  await shoot(page, "approvals");
  await page.getByRole("button", { name: "Review exact changes", exact: true }).first().click();
  await page.getByTestId("approve-change").click();
  await page.getByTestId("confirm-approval").click();
  await expect(page.getByText("Verified by independent read", { exact: true })).toBeVisible({
    timeout: 30_000,
  });
  await shoot(page, "execution");
  await shoot(page, "records");
  await shoot(page, "recovery");
  await shoot(page, "overview");
  await shoot(page, "inbox");
  await signOut(page);

  await signIn(page, "auditor");
  await shoot(page, "evidence");
  await shoot(page, "activity");
  await signOut(page);

  await signIn(page, "admin");
  await shoot(page, "settings");
});
