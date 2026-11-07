import { test, expect } from "@playwright/test";

test("real local agent autonomously prepares the pending proposal in the nonowner browser profile", async ({
  page,
}) => {
  test.skip(
    process.env.FOLIO_REAL_MODEL_TEST !== "1",
    "explicit admitted real local runtime required",
  );
  await page.goto("/");
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
    .getByRole("button", { name: "Import authored sample packet", exact: true })
    .click();
  await page
    .getByLabel("Allow agent to prepare a review-only proposal")
    .check();
  await page
    .getByRole("button", { name: "Run local model analysis", exact: true })
    .click();
  await expect(page.getByTestId("analysis-status")).toContainText("prepared", {
    timeout: 20000,
  });
  await page
    .getByRole("button", {
      name: "Inspect agent-prepared outcome",
      exact: true,
    })
    .click();
  await expect(
    page.getByRole("heading", { name: "Review proposed changes", exact: true }),
  ).toBeVisible();
  await expect(page.getByTestId("retained-balance")).toHaveText("USD 1036.80");
  await expect(page.getByText("5184.00", { exact: true })).toBeVisible();
  await expect(page.getByTestId("approve-change")).toBeDisabled();
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
    .click();
  await page.getByTestId("approve-change").click();
  await page.getByTestId("confirm-approval").click();
  await expect(
    page.getByText("Verified by independent read", { exact: true }),
  ).toBeVisible({ timeout: 20000 });
  await page
    .getByRole("button", { name: "View business record", exact: true })
    .click();
  await expect(
    page.getByText("Historical authorized effect verified", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("5184.00", { exact: true }).first(),
  ).toBeVisible();
  await page.screenshot({
    path: "../evidence/documents/model/local-smoke/autonomous-browser.png",
    fullPage: true,
  });
});
