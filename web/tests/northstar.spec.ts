import { test, expect } from "@playwright/test";
test("independent Northstar console reads actual committed PostgreSQL record", async ({
  page,
}) => {
  await page.goto(
    (process.env.FOLIO_TARGET_URL || "http://127.0.0.1:8201") + "/",
  );
  await expect(
    page.getByRole("heading", { name: "Northstar Ledger", exact: true }),
  ).toBeVisible();
  await page.getByLabel("Username").fill("operator");
  const password = process.env.FOLIO_TARGET_OPERATOR_PASSWORD;
  if (!password) throw new Error("FOLIO_TARGET_OPERATOR_PASSWORD is required");
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  const invoice = page
    .getByRole("row")
    .filter({
      has: page.getByRole("cell", {
        name: "INV-1048 / 22222222-2222-4222-8222-222222222222",
        exact: true,
      }),
    });
  await expect(
    invoice.getByRole("cell", { name: "5184.00", exact: true }),
  ).toBeVisible();
  await expect(
    invoice.getByRole("cell", { name: "6220.80", exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: "../evidence/documents/flow/northstar-console.png",
    fullPage: true,
  });
});
