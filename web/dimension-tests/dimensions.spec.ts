import { expect, test } from "@playwright/test";

const fixtures = [
  {
    currency: "EUR",
    unit: "kg",
    face: "24.69",
    retained: "12.34",
    quantum: "EUR 0.01",
    quantity: "10.25",
  },
  {
    currency: "JPY",
    unit: "m",
    face: "2469",
    retained: "1234",
    quantum: "JPY 1",
    quantity: "10.25",
  },
  {
    currency: "KWD",
    unit: "box",
    face: "24.690",
    retained: "12.345",
    quantum: "KWD 0.001",
    quantity: "10.25",
  },
];

for (const fixture of fixtures) {
  test(`${fixture.currency}/${fixture.unit} dimensions stay visible across human review`, async ({
    page,
  }) => {
    const row = {
      key: "GR-UI:1",
      receipt_id: "GR-UI",
      line_id: "1",
      quantity: fixture.quantity,
      allocated: "5",
      currency: fixture.currency,
      unit: fixture.unit,
      evidence: [],
    };
    const proposal = {
      id: "proposal-ui-dimensions",
      version: 1,
      state: "pending",
      patch: {
        recognized_quantity: "5",
        net_amount: "5",
        tax_amount: "0",
        gross_amount: "5",
        status: "partial",
      },
      digest: "a".repeat(64),
      target_digest: "b".repeat(64),
      target_revision: 1,
      operation_key: "op-ui-dimensions",
      prepared_by: "reviewer",
      reason: "Authored dimension display fixture",
      before_snapshot: {},
      allocation_manifest: {
        rows: [row],
        total: "5",
        policy: "receipt-allocation-v1",
      },
      retained_balance: fixture.retained,
      expires: "2030-01-01T00:00:00+00:00",
    };
    const item = {
      id: "case-ui-dimensions",
      label: "Authored dimensions",
      invoice: `INV-${fixture.currency}`,
      supplier: "Example Supplier",
      state: "awaiting_approval",
      version: 1,
      sources: [],
      source_digest: "c".repeat(64),
      invoice_quantity: "20",
      ordered_quantity: "20",
      received_quantity: fixture.quantity,
      unit_price: "1.234",
      tax_rate: "0",
      invoice_face_amount: fixture.face,
      currency: fixture.currency,
      unit: fixture.unit,
      proposal,
      verification: null,
      receipt_manifest: { rows: [row], total: fixture.quantity, errors: [] },
      sample_digest: "d".repeat(64),
    };

    await page.route("**/api/v1/auth/session", (route) =>
      route.fulfill({ status: 401, json: { detail: "SESSION_REQUIRED" } }),
    );
    await page.route("**/api/v1/auth/login", (route) =>
      route.fulfill({
        status: 200,
        json: {
          username: "reviewer",
          roles: ["reviewer"],
          csrf: "authored-csrf",
          workspace: "authored-workspace",
        },
      }),
    );
    await page.route(
      "**/api/v1/workspaces/authored-workspace/**",
      async (route) => {
        const url = new URL(route.request().url());
        if (url.pathname.endsWith("/events")) {
          await route.fulfill({
            status: 200,
            contentType: "text/event-stream",
            body: ": authored test stream\n\n",
          });
        } else if (url.pathname.endsWith("/cases")) {
          await route.fulfill({
            status: 200,
            json: { projection: "queue-summary-v1", items: [item] },
          });
        } else if (url.pathname.endsWith(`/cases/${item.id}`)) {
          await route.fulfill({ status: 200, json: item });
        } else {
          await route.fulfill({ status: 404, json: { detail: "NOT_FOUND" } });
        }
      },
    );

    await page.goto("/");
    await page.getByLabel("Username").fill("reviewer");
    await page.getByLabel("Password", { exact: true }).fill("authored-only");
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await page.getByRole("button", { name: "Open packet" }).click();

    await expect(
      page.getByText(`${fixture.currency} ${fixture.face}`),
    ).toBeVisible();
    await expect(page.getByText(`20 ${fixture.unit}`)).toBeVisible();
    await expect(
      page
        .getByRole("navigation", { name: "Workspace navigation" })
        .getByRole("button", { name: "Reconciliation workspace" }),
    ).toBeVisible();
    await page
      .getByRole("navigation", { name: "Workspace navigation" })
      .getByRole("button", { name: "Reconciliation workspace" })
      .click();
    await expect(page.getByTestId("receipt-lines")).toContainText(
      `${fixture.quantity} ${fixture.unit} · ${fixture.currency}`,
    );
    await expect(
      page.getByText(fixture.quantum, { exact: false }),
    ).toBeVisible();
    await expect(page.getByText(`Quantity · ${fixture.unit}`)).toBeVisible();

    await page
      .getByRole("navigation", { name: "Workspace navigation" })
      .getByRole("button", { name: "Resolve an exception" })
      .click();
    await expect(
      page.getByText(
        `Source receipt total: ${fixture.quantity} ${fixture.unit}`,
      ),
    ).toBeVisible();
    await expect(
      page.getByText(
        `Original invoice face: ${fixture.currency} ${fixture.face}`,
      ),
    ).toBeVisible();

    await page
      .getByRole("navigation", { name: "Workspace navigation" })
      .getByRole("button", { name: "Review proposed changes" })
      .click();
    await expect(page.getByTestId("retained-balance")).toHaveText(
      `${fixture.currency} ${fixture.retained}`,
    );
    await expect(page.getByTestId("allocation-manifest")).toContainText(
      `5 ${fixture.unit}`,
    );
    await expect(page.getByTestId("allocation-manifest")).toContainText(
      `5 of ${fixture.quantity} ${fixture.unit}`,
    );
  });
}
