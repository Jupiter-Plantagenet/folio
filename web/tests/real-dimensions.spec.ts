import { expect, test } from "@playwright/test";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";

test.use({ launchOptions: { channel: "chrome" } });

type DimensionCase = {
  invoice: string;
  currency: string;
  unit: string;
  unitPrice: string;
  targetId: string;
  files: Array<{ name: string; role: string; path: string; sha256: string }>;
  face: string;
  quantum: string;
  retained: string;
};

const cases: Omit<DimensionCase, "targetId">[] = [
  {
    invoice: "INV-DIM-EUR-KG",
    currency: "EUR",
    unit: "kg",
    unitPrice: "48.00",
    face: "6220.80",
    quantum: "EUR 0.01",
    retained: "EUR 1082.16",
  },
  {
    invoice: "INV-DIM-JPY-M",
    currency: "JPY",
    unit: "m",
    unitPrice: "48",
    face: "6221",
    quantum: "JPY 1",
    retained: "JPY 1082",
  },
  {
    invoice: "INV-DIM-KWD-BOX",
    currency: "KWD",
    unit: "box",
    unitPrice: "48.125",
    face: "6237.000",
    quantum: "KWD 0.001",
    retained: "KWD 1084.978",
  },
];

const fixturePath = process.env.FOLIO_DIMENSION_FIXTURE_MANIFEST;
if (!fixturePath)
  throw new Error("FOLIO_DIMENSION_FIXTURE_MANIFEST is required");
const seeded = JSON.parse(readFileSync(fixturePath, "utf8")) as Array<{
  target_id: string;
  invoice: string;
  currency: string;
  unit: string;
  files: Array<{ name: string; role: string; path: string; sha256: string }>;
}>;
expect(seeded).toHaveLength(cases.length);
const fixtures: DimensionCase[] = cases.map((expected) => {
  const target = seeded.find((item) => item.invoice === expected.invoice);
  expect(target).toBeDefined();
  expect(target).toMatchObject({
    currency: expected.currency,
    unit: expected.unit,
  });
  return { ...expected, targetId: target!.target_id, files: target!.files };
});

async function signIn(page, username: "reviewer" | "approver") {
  await page.getByLabel("Username").fill(username);
  await page
    .getByLabel("Password", { exact: true })
    .fill(
      username === "reviewer" ? "folio-reviewer-local" : "folio-approver-local",
    );
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("navigation", { name: "Workspace navigation" }),
  ).toBeVisible();
}

async function correctField(
  page,
  field: string,
  value: string,
  reason: string,
) {
  await page.getByLabel("Field to correct").selectOption(field);
  await page.getByLabel("Corrected value").fill(value);
  await page.getByLabel("Correction reason").fill(reason);
  await page.getByTestId("save-general-field").click();
  await expect(page.getByRole("alert")).toHaveCount(0);
}

test("real API and PostgreSQL dimension cases pass through human approval and verification", async ({
  page,
}) => {
  test.setTimeout(120_000);
  const apiResponses: Array<{ status: number; url: string }> = [];
  const caseIds: string[] = [];
  const parserImages = new Set<string>();
  page.on("response", (response) => {
    if (new URL(response.url()).pathname.startsWith("/api/v1/")) {
      apiResponses.push({
        status: response.status(),
        url: new URL(response.url()).pathname,
      });
    }
  });
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Sign in to Folio" }),
  ).toBeVisible();
  await signIn(page, "reviewer");
  const nav = page.getByRole("navigation", { name: "Workspace navigation" });
  for (const fixture of fixtures) {
    await nav
      .getByRole("button", { name: "Import documents", exact: true })
      .click();
    const uploadFiles = fixture.files.map((file) => {
      const bytes = readFileSync(file.path);
      expect(createHash("sha256").update(bytes).digest("hex")).toBe(
        file.sha256,
      );
      return { name: file.name, mimeType: "application/pdf", buffer: bytes };
    });
    await page
      .getByLabel("Local synthetic files")
      .setInputFiles([...uploadFiles]);
    await page
      .getByLabel("Document role for invoice.pdf")
      .selectOption("invoice");
    await page
      .getByLabel("Document role for purchase-order.pdf")
      .selectOption("purchase_order");
    await page
      .getByLabel("Document role for receipt.pdf")
      .selectOption("receipt");
    await page.getByLabel("I authored these synthetic documents").check();
    const intakeResponse = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname.endsWith("/intakes/files") &&
        response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Import selected files" }).click();
    const imported = await intakeResponse;
    expect(imported.status()).toBe(202);
    const intake = await imported.json();
    expect(intake.accepted).toHaveLength(3);
    expect(intake.rejected).toEqual([]);
    expect(typeof intake.case_id).toBe("string");
    caseIds.push(intake.case_id);
    await expect(
      page.getByRole("heading", { name: "Document & extracted fields" }),
    ).toBeVisible();
    await expect(page.getByTestId("invoice-field")).toHaveValue(
      fixture.invoice,
    );
    const importedCase = await page.evaluate(async (caseId) => {
      const response = await fetch(
        `/api/v1/workspaces/11111111-1111-4111-8111-111111111111/cases/${caseId}`,
      );
      if (!response.ok)
        throw new Error(`case detail failed: ${response.status}`);
      return response.json();
    }, intake.case_id);
    expect(importedCase.sources).toHaveLength(3);
    for (const source of importedCase.sources) {
      if (typeof source.containment?.image_id === "string") {
        parserImages.add(source.containment.image_id);
      }
    }
    expect(
      importedCase.sources.every(
        (source: {
          containment?: { network?: string; read_only_root?: boolean };
        }) =>
          source.containment?.network === "none" &&
          source.containment?.read_only_root === true,
      ),
    ).toBe(true);
    if (
      importedCase.currency !== fixture.currency ||
      importedCase.conflicting_fields?.includes("currency")
    ) {
      await correctField(
        page,
        "currency",
        fixture.currency,
        "Read the authored invoice currency declaration and confirm KWD explicitly",
      );
    }
    if (
      importedCase.unit !== fixture.unit ||
      importedCase.conflicting_fields?.includes("unit")
    ) {
      await correctField(
        page,
        "unit",
        fixture.unit,
        `Read the authored receipt unit declaration and confirm ${fixture.unit}`,
      );
    }
    if (
      importedCase.supplier !== "Aster Supply Co." ||
      importedCase.conflicting_fields?.includes("supplier")
    ) {
      await correctField(
        page,
        "supplier",
        "Aster Supply Co.",
        "Read the authored purchase-order supplier name and resolve the issuer match",
      );
    }
    // Money fields are reported only when they reconcile exactly (confirm-or-refuse);
    // otherwise the reviewer enters every one of them, not just the ambiguous ones.
    if (
      importedCase.invoice_quantity !== "120" ||
      importedCase.conflicting_fields?.includes("invoice_quantity")
    ) {
      await correctField(
        page,
        "invoice_quantity",
        "120",
        "Read the authored invoiced quantity",
      );
    }
    if (
      importedCase.tax_rate !== "0.08" ||
      importedCase.conflicting_fields?.includes("tax_rate")
    ) {
      await correctField(
        page,
        "tax_rate",
        "0.08",
        "Read the authored 8% tax rate",
      );
    }
    if (
      importedCase.unit_price !== fixture.unitPrice ||
      importedCase.conflicting_fields?.includes("unit_price")
    ) {
      await correctField(
        page,
        "unit_price",
        fixture.unitPrice,
        `Read the authored ${fixture.currency} unit price at its printed precision`,
      );
    }
    if (
      importedCase.invoice_face_amount !== fixture.face ||
      importedCase.conflicting_fields?.includes("invoice_face_amount")
    ) {
      await correctField(
        page,
        "invoice_face_amount",
        fixture.face,
        `Read the authored ${fixture.currency} invoice face at its printed precision`,
      );
    }
    await expect(
      page
        .getByText(`${fixture.currency} ${fixture.face}`, { exact: false })
        .first(),
    ).toBeVisible();
    await expect(
      page.getByText(`100 ${fixture.unit}`, { exact: false }).first(),
    ).toBeVisible();

    await nav.getByRole("button", { name: "Reconciliation workspace" }).click();
    await expect(page.getByTestId("receipt-lines")).toContainText(
      `${fixture.currency}`,
    );
    await expect(page.getByTestId("receipt-lines")).toContainText(
      `${fixture.unit}`,
    );
    await expect(
      page.getByText(fixture.quantum, { exact: false }),
    ).toBeVisible();
    await page
      .getByRole("button", { name: "Look up matching payable" })
      .click();
    const targetChoice = page.getByLabel("Matching target candidates");
    await expect(targetChoice).toBeVisible();
    await targetChoice.selectOption(fixture.targetId);
    await page
      .getByRole("button", { name: "Look up matching payable" })
      .click();
    await expect(
      page.getByRole("heading", { name: "Reconciliation workspace" }),
    ).toBeVisible();

    await nav.getByRole("button", { name: "Resolve an exception" }).click();
    await expect(
      page.getByText(`Source receipt total: 100 ${fixture.unit}`),
    ).toBeVisible();
    await expect(
      page.getByText(
        `Original invoice face: ${fixture.currency} ${fixture.face}`,
      ),
    ).toBeVisible();
    await page.getByLabel("Recognized quantity").fill("99.125");
    await page
      .getByLabel("Decision reason")
      .fill(
        `Reviewed authored ${fixture.currency}/${fixture.unit} receipt support`,
      );
    await page.getByTestId("save-decision").click();
    await expect(
      page.getByRole("heading", { name: "Review proposed changes" }),
    ).toBeVisible();
    await expect(page.getByTestId("retained-balance")).toHaveText(
      fixture.retained,
    );
    await expect(page.getByTestId("allocation-manifest")).toContainText(
      `99.125 ${fixture.unit}`,
    );
    await expect(page.getByTestId("allocation-manifest")).toContainText(
      fixture.currency,
    );
  }

  const screenshotPath = process.env.FOLIO_DIMENSION_SCREENSHOT_PATH;
  if (!screenshotPath)
    throw new Error("FOLIO_DIMENSION_SCREENSHOT_PATH is required");
  await page.screenshot({ path: screenshotPath, fullPage: true });
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await signIn(page, "approver");
  const approvalNav = page.getByRole("navigation", {
    name: "Workspace navigation",
  });
  for (const fixture of fixtures) {
    await approvalNav.getByRole("button", { name: "Approval queue" }).click();
    const task = page.locator(".task").filter({ hasText: fixture.invoice });
    await expect(task).toBeVisible();
    await task.getByRole("button", { name: "Review exact changes" }).click();
    await expect(
      page.getByRole("heading", { name: "Review proposed changes" }),
    ).toBeVisible();
    await expect(page.getByTestId("allocation-manifest")).toContainText(
      fixture.unit,
    );
    await expect(page.getByTestId("allocation-manifest")).toContainText(
      fixture.currency,
    );
    await page.getByTestId("approve-change").click();
    await page.getByTestId("confirm-approval").click();
    await expect(
      page.getByRole("heading", { name: "Execution detail" }),
    ).toBeVisible();
    await expect(
      page.getByText("Verified by independent read", { exact: true }),
    ).toBeVisible({ timeout: 30000 });
    await page
      .getByRole("button", { name: "View business record", exact: true })
      .click();
    await expect(
      page.getByRole("heading", { name: "Verified business records" }),
    ).toBeVisible();
    const snapshotHeading = page.getByRole("heading", {
      name: "Independent target snapshot",
    });
    await expect(snapshotHeading).toBeVisible();
    const snapshot = snapshotHeading.locator("xpath=../..");
    await expect(snapshot).toContainText(fixture.currency);
    await expect(snapshot).toContainText(fixture.unit);
  }

  expect(apiResponses.length).toBeGreaterThan(20);
  const unexpectedFailures = apiResponses.filter(
    (response) =>
      response.status >= 400 &&
      !(response.url === "/api/v1/auth/session" && response.status === 401),
  );
  expect(unexpectedFailures).toEqual([]);
  await page.getByRole("button", { name: "Sign out", exact: true }).click();

  const racePage = await page.context().newPage();
  const raceResponses: Array<{ status: number; url: string }> = [];
  racePage.on("response", (response) => {
    if (new URL(response.url()).pathname.startsWith("/api/v1/")) {
      raceResponses.push({
        status: response.status(),
        url: new URL(response.url()).pathname,
      });
    }
  });
  await racePage.goto("/");
  await signIn(racePage, "reviewer");
  const raceNav = racePage.getByRole("navigation", {
    name: "Workspace navigation",
  });
  await raceNav.getByRole("button", { name: "Document inbox" }).click();

  let releaseDelayedCase: () => void = () => {};
  let delayedCaseFetched: () => void = () => {};
  let delayedCaseFulfilled: () => void = () => {};
  const holdDelayedCase = new Promise<void>((resolve) => {
    releaseDelayedCase = resolve;
  });
  const actualCaseFetched = new Promise<void>((resolve) => {
    delayedCaseFetched = resolve;
  });
  const delayedCaseDelivered = new Promise<void>((resolve) => {
    delayedCaseFulfilled = resolve;
  });
  await racePage.route("**/api/v1/workspaces/*/cases/*", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() === "GET" && path.endsWith(`/${caseIds[0]}`)) {
      const response = await route.fetch();
      delayedCaseFetched();
      await holdDelayedCase;
      await route.fulfill({ response });
      delayedCaseFulfilled();
      return;
    }
    await route.continue();
  });
  try {
    const eurRow = racePage
      .getByRole("row")
      .filter({ hasText: cases[0].invoice });
    const jpyRow = racePage
      .getByRole("row")
      .filter({ hasText: cases[1].invoice });
    await eurRow.getByRole("button", { name: "Open packet" }).click();
    await actualCaseFetched;
    await jpyRow.getByRole("button", { name: "Open packet" }).click();
    await expect(racePage.getByTestId("invoice-field")).toHaveValue(
      cases[1].invoice,
    );
    releaseDelayedCase();
    await delayedCaseDelivered;
    await expect(racePage.getByTestId("invoice-field")).toHaveValue(
      cases[1].invoice,
    );
  } finally {
    releaseDelayedCase();
  }
  const raceFailures = raceResponses.filter(
    (response) =>
      response.status >= 400 &&
      !(response.url === "/api/v1/auth/session" && response.status === 401),
  );
  expect(raceFailures).toEqual([]);
  expect(raceResponses.some((response) => response.status === 200)).toBe(true);
  await racePage.close();

  expect(parserImages.size).toBe(1);
  const evidencePath = process.env.FOLIO_DIMENSION_API_EVIDENCE;
  if (!evidencePath)
    throw new Error("FOLIO_DIMENSION_API_EVIDENCE is required");
  writeFileSync(
    evidencePath,
    JSON.stringify(
      {
        actual_ui_api: true,
        mocked_api_responses: 0,
        pass_through_response_delays: 1,
        case_switch_stale_response_check: true,
        browser_version: page.context().browser()?.version() ?? "unknown",
        successful_cases: caseIds,
        parser_image_ids: [...parserImages],
        api_responses: [...apiResponses, ...raceResponses],
        authored_inputs: fixtures.map((fixture) => ({
          invoice: fixture.invoice,
          sources: fixture.files.map(({ name, sha256 }) => ({ name, sha256 })),
        })),
      },
      null,
      2,
    ),
    "utf8",
  );
  await page.evaluate((responses) => {
    document.documentElement.dataset.realDimensionApiResponses =
      JSON.stringify(responses);
  }, apiResponses);
});
