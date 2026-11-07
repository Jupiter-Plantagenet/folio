import { test, expect } from "@playwright/test";

async function signIn(page: any, name: string) {
  await page.goto("/");
  await page.getByLabel("Username").fill(name);
  await page.getByLabel("Password", { exact: true }).fill(`folio-${name}-local`);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.locator("#screen")).toBeVisible();
}

const nav = (page: any, name: string) =>
  page
    .getByRole("navigation", { name: "Workspace navigation" })
    .getByRole("button", { name, exact: true })
    .click();

test("provenance highlight, visible human correction, refresh-safe draft, pending save, dialog focus", async ({
  page,
}) => {
  await signIn(page, "reviewer");
  await nav(page, "Import documents");
  await page.getByRole("button", { name: "Import authored sample packet" }).click();

  // Provenance: clicking a parsed source word highlights it on the source.
  const firstWord = page.locator(".source-text .source-word").first();
  await expect(firstWord).toBeVisible();
  await firstWord.click();
  await expect(firstWord).toHaveClass(/selected/);
  const highlight = await firstWord.evaluate((el) => getComputedStyle(el).backgroundColor);
  expect(highlight).not.toBe("rgba(0, 0, 0, 0)");

  // A human correction is visibly distinct from parser-extracted values.
  await page.getByLabel("Field to correct").selectOption("invoice_quantity");
  await page.getByLabel("Corrected value").fill("120");
  await page.getByLabel("Correction reason").fill("Confirmed with supplier statement");
  await page.getByTestId("save-general-field").click();
  await expect(page.getByTestId("human-corrected-invoice_quantity")).toBeVisible();
  await expect(page.getByTestId("human-correction-entry").first()).toContainText(
    "Confirmed with supplier statement",
  );

  // An unsent decision draft survives a page refresh.
  await page.getByRole("button", { name: "Compare documents", exact: true }).click();
  await page.getByRole("button", { name: "Resolve exception", exact: true }).click();
  await page.getByLabel("Recognized quantity").fill("100");
  await page.getByLabel("Decision reason").fill("DRAFT-SURVIVES-REFRESH");
  await page.reload();
  await expect(page.locator("#screen")).toBeVisible();
  await expect(page.locator("main")).toHaveAttribute("data-current-screen", "decision");
  await expect(page.getByLabel("Decision reason")).toHaveValue("DRAFT-SURVIVES-REFRESH");
  await expect(page.getByLabel("Recognized quantity")).toHaveValue("100");

  // A protected save shows pending until the server answers; no early success.
  let release!: () => void;
  const held = new Promise<void>((resolve) => (release = resolve));
  await page.route("**/api/v1/workspaces/*/cases/*/decisions", async (route) => {
    await held;
    await route.continue();
  });
  await page.getByTestId("save-decision").click();
  await expect(page.getByTestId("save-decision")).toBeDisabled();
  await page.waitForTimeout(500);
  await expect(page.locator("main")).toHaveAttribute("data-current-screen", "decision");
  await expect(page.getByRole("heading", { name: "Review proposed changes" })).toHaveCount(0);
  release();
  await expect(page.getByRole("heading", { name: "Review proposed changes" })).toBeVisible();
  await page.unroute("**/api/v1/workspaces/*/cases/*/decisions");

  // Dialog returns keyboard focus to the control that opened it.
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  // Leaving mid-request would (correctly) leave an unresolved logout to reconcile.
  await expect(page.getByRole("heading", { name: "Sign in to Folio" })).toBeVisible();
  await signIn(page, "approver");
  await nav(page, "Approval queue");
  await page.getByRole("button", { name: "Review exact changes", exact: true }).first().click();
  const opener = page.getByTestId("approve-change");
  await opener.focus();
  await opener.click();
  const dialog = page.locator("dialog[open]");
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(opener).toBeFocused();
});
