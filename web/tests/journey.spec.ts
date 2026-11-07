import { test, expect } from "@playwright/test";
test("real authored packet to separate approval and independent target verification", async ({
  page,
}) => {
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Sign in to Folio" }),
  ).toBeVisible();
  await page.getByLabel("Username").fill("reviewer");
  await page
    .getByLabel("Password", { exact: true })
    .fill("folio-reviewer-local");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await page
    .getByRole("navigation", { name: "Workspace navigation" })
    .getByRole("button", { name: "Import documents", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Import authored sample packet" })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Document & extracted fields",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByText("Invoice INV-1048", { exact: false }).first(),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Compare documents", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Resolve exception", exact: true })
    .click();
  await page
    .getByLabel("Decision reason")
    .fill("Browser-reviewed receipt supports only 100 units");
  await page.getByTestId("save-decision").click();
  await expect(
    page.getByRole("heading", { name: "Review proposed changes" }),
  ).toBeVisible();
  await expect(page.getByTestId("retained-balance")).toHaveText("USD 1036.80");
  await page.screenshot({
    path: "../evidence/documents/flow/exact-diff.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await page.getByLabel("Username").fill("approver");
  await page
    .getByLabel("Password", { exact: true })
    .fill("folio-approver-local");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await page
    .getByRole("button", { name: "Approval queue", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Review exact changes", exact: true })
    .first()
    .click();
  await page.getByTestId("approve-change").click();
  await page.getByTestId("confirm-approval").click();
  await expect(
    page.getByRole("heading", { name: "Execution detail" }),
  ).toBeVisible();
  await expect(
    page.getByText("Verified by independent read", { exact: true }),
  ).toBeVisible({ timeout: 20000 });
  await page
    .getByRole("button", { name: "View business record", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Verified business records" }),
  ).toBeVisible();
  await expect(
    page.getByText("5184.00", { exact: true }).first(),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByText("5184.00", { exact: true }).first(),
  ).toBeVisible();
  await page.screenshot({
    path: "../evidence/documents/flow/records-desktop.png",
    fullPage: true,
  });
  for (const width of [1440, 1024, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await expect
      .poll(() =>
        page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      )
      .toBeTruthy();
    await page.screenshot({
      path: `../evidence/documents/ux/records-${width}.png`,
      fullPage: true,
    });
  }
});
