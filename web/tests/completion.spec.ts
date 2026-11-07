import { test, expect } from "@playwright/test";

test("intake limits reject excessive selections before reading any file", async ({ page }) => {
  await page.addInitScript(() => {
    const original = FileReader.prototype.readAsDataURL;
    (window as any).__fileReads = 0;
    FileReader.prototype.readAsDataURL = function (...args: Parameters<typeof original>) {
      (window as any).__fileReads += 1;
      return original.apply(this, args);
    };
  });
  let intakePosts = 0;
  await page.route("**/api/v1/workspaces/*/intakes/files", async (route) => {
    intakePosts += 1;
    await route.continue();
  });
  await page.goto("/");
  await page.getByLabel("Username").fill("reviewer");
  await page.getByLabel("Password", { exact: true }).fill("folio-reviewer-local");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await page.getByRole("navigation").getByRole("button", { name: "Import documents", exact: true }).click();
  await page.getByLabel("Local synthetic files").setInputFiles(
    Array.from({ length: 11 }, (_, index) => ({
      name: `receipt-${index}.csv`,
      mimeType: "text/csv",
      buffer: Buffer.from("Received quantity,100\nUnit,each\n"),
    })),
  );
  await page.getByLabel("I authored these synthetic documents").check();
  await page.getByRole("button", { name: "Import selected files", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Select at most 10 files");
  expect(await page.evaluate(() => (window as any).__fileReads)).toBe(0);
  expect(intakePosts).toBe(0);
  await page.getByLabel("Local synthetic files").setInputFiles({
    name: "oversized.csv",
    mimeType: "text/csv",
    buffer: Buffer.alloc(20 * 1024 * 1024 + 1, 65),
  });
  await page.getByRole("button", { name: "Import selected files", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("exceeds the 20 MiB per-file limit");
  expect(await page.evaluate(() => (window as any).__fileReads)).toBe(0);
  await page.getByLabel("Local synthetic files").setInputFiles({
    name: "unsupported.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("not a supported document"),
  });
  await page.getByRole("button", { name: "Import selected files", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("not a supported PDF, CSV, or XLSX file");
  expect(await page.evaluate(() => (window as any).__fileReads)).toBe(0);
  expect(intakePosts).toBe(0);
});

test("sandboxed CSV intake, general correction, and honest unavailable model state", async ({
  page,
}) => {
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
  await page.getByLabel("Local synthetic files").setInputFiles({
    name: "receipt.csv",
    mimeType: "text/csv",
    buffer: Buffer.from("Received quantity,100\nUnit,each\n"),
  });
  await page
    .getByLabel("Document role for receipt.csv")
    .selectOption("receipt");
  await page.getByLabel("I authored these synthetic documents").check();
  await page
    .getByRole("button", { name: "Import selected files", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Document & extracted fields",
      exact: true,
    }),
  ).toBeVisible();
  await page.getByLabel("Field to correct").selectOption("received_quantity");
  await page.getByLabel("Corrected value", { exact: true }).fill("99.5");
  await page
    .getByLabel("Correction reason")
    .fill("Reviewed synthetic goods receipt");
  await page.getByTestId("save-general-field").click();
  await expect(page.getByText("99.5 each", { exact: true })).toBeVisible();
  await page
    .getByRole("button", { name: "Run local model analysis", exact: true })
    .click();
  await expect(page.getByTestId("analysis-status")).toContainText("blocked", {
    timeout: 15000,
  });
  await expect(
    page.getByText("No target writes from analysis.", { exact: true }),
  ).toBeVisible();
});

test("independent approver can reject an exact proposal without a target effect", async ({
  page,
}) => {
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
    .getByRole("button", { name: "Compare documents", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Resolve exception", exact: true })
    .click();
  await page
    .getByLabel("Decision reason")
    .fill("Prepare a proposal to test independent rejection");
  await page.getByTestId("save-decision").click();
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
  await page
    .getByLabel("Authorization action reason")
    .fill("Quantity evidence requires another review");
  await page
    .getByRole("button", { name: "Reject exact proposal", exact: true })
    .click();
  await expect(page.getByText("rejected", { exact: true })).toBeVisible();
});
