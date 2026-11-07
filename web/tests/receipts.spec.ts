import { test, expect } from "@playwright/test";
import path from "node:path";
import fs from "node:fs";

test("missing supporting receipt resumes source-linked two-receipt review", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByLabel("Username").fill("reviewer");
  await page
    .getByLabel("Password", { exact: true })
    .fill("folio-reviewer-local");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  const nav = page.getByRole("navigation", { name: "Workspace navigation" });
  await nav
    .getByRole("button", { name: "Import documents", exact: true })
    .click();
  await page.getByLabel("Local synthetic files").setInputFiles([
    {
      name: "receipt-invoice.csv",
      mimeType: "text/csv",
      buffer: Buffer.from(
        "Invoice INV-RECEIPTS\nQuantity,120\nUnit price USD,48.00\nTax rate,8%\nInvoice face total USD,6220.80\n",
      ),
    },
    {
      name: "purchase-order.pdf",
      mimeType: "application/pdf",
      buffer: fs.readFileSync(
        path.resolve("../fixtures/public/purchase-order.pdf"),
      ),
    },
  ]);
  await page
    .getByLabel("Document role for receipt-invoice.csv")
    .selectOption("invoice");
  await page
    .getByLabel("Document role for purchase-order.pdf")
    .selectOption("purchase_order");
  await page.getByLabel("I authored these synthetic documents").check();
  await page
    .getByRole("button", { name: "Import selected files", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Compare documents", exact: true })
    .click();
  await page
    .getByLabel("Information request reason")
    .fill("Please provide the two independently received lines");
  await page
    .getByRole("button", { name: "Request missing information", exact: true })
    .click();
  await expect(page.getByTestId("information-requests")).toContainText("open");
  await page
    .getByRole("button", { name: "Attach supporting documents", exact: true })
    .click();
  await page.getByLabel("Local synthetic files").setInputFiles([
    {
      name: "first.csv",
      mimeType: "text/csv",
      buffer: Buffer.from(
        "Receipt,GR-BROWSER-A\nLine,1\nReceived quantity,40.1\nUnit,each\nCurrency,USD\n",
      ),
    },
    {
      name: "second.csv",
      mimeType: "text/csv",
      buffer: Buffer.from(
        "Receipt,GR-BROWSER-B\nLine,1\nReceived quantity,59.9\nUnit,each\nCurrency,USD\n",
      ),
    },
  ]);
  await page.getByLabel("Document role for first.csv").selectOption("receipt");
  await page.getByLabel("Document role for second.csv").selectOption("receipt");
  await page
    .getByRole("button", { name: "Import selected files", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Compare documents", exact: true })
    .click();
  await expect(page.getByTestId("receipt-lines")).toContainText("40.1");
  await expect(page.getByTestId("receipt-lines")).toContainText("59.9");
  await expect(page.getByTestId("information-requests")).toContainText(
    "resolved",
  );
  await page
    .getByRole("button", { name: "Look up matching payable", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Resolve exception", exact: true })
    .click();
  await page.getByLabel("Recognized quantity").fill("100.000001");
  await page
    .getByLabel("Decision reason")
    .fill("Retain unsupported invoice balance");
  await page.getByTestId("save-decision").click();
  await expect(page.getByRole("alert")).toContainText("QUANTITY_EXCEEDS");
  await page.getByLabel("Recognized quantity").fill("100");
  await page.getByTestId("save-decision").click();
  await expect(page.getByTestId("retained-balance")).toHaveText("USD 1036.80");
  await expect(page.getByTestId("allocation-manifest")).toContainText("40.1");
  await expect(page.getByTestId("allocation-manifest")).toContainText("59.9");
  await page.screenshot({
    path: "../evidence/documents/flow/receipt-allocations.png",
    fullPage: true,
  });
});
