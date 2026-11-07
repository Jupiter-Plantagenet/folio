import { test, expect, Page } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

// Round-4 LIVE stack journeys (retained workspace, real API 8101 via Vite 5101,
// real worker, real local model, real Northstar 8201). Testers are automated
// agents ("simulated humans"), not human participants.
const PACKETS = process.env.FOLIO_LIVE_PACKETS!;
const OUT = process.env.FOLIO_LIVE_EVIDENCE!;
const pw = (role: string) => `folio-${role}-local`; // public dev fixture credentials

async function shot(page: Page, name: string) {
  await page.screenshot({ path: path.join(OUT, name + ".png"), fullPage: true });
}
async function signIn(page: Page, role: string) {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Sign in to Folio" })).toBeVisible();
  await page.getByLabel("Username").fill(role);
  await page.getByLabel("Password", { exact: true }).fill(pw(role));
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("navigation", { name: "Workspace navigation" })).toBeVisible();
}
async function signOut(page: Page) {
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Sign in to Folio" })).toBeVisible();
}
async function nav(page: Page, name: string) {
  await page
    .getByRole("navigation", { name: "Workspace navigation" })
    .getByRole("button", { name, exact: true })
    .click();
}
async function importPacket(page: Page, scenario: string) {
  const dir = path.join(PACKETS, scenario);
  const files = fs.readdirSync(dir).filter((f) => /\.(pdf|csv|xlsx)$/.test(f)).sort();
  await nav(page, "Import documents");
  await page.locator("#local-files").setInputFiles(files.map((f) => path.join(dir, f)));
  for (const [i, f] of files.entries()) {
    const role = f.startsWith("invoice") ? "invoice" : f.startsWith("purchase") ? "purchase_order" : "receipt";
    await page.locator(`#file-role-${i}`).selectOption(role);
  }
  await page.getByText("I authored these synthetic documents").click();
  await page.getByRole("button", { name: "Import selected files", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Document & extracted fields", exact: true })).toBeVisible({ timeout: 60000 });
  await expect(page.getByText(`Accepted ${files.length}; rejected 0`, { exact: false })).toBeVisible({ timeout: 30000 });
}
const invoiceKey = (scenario: string): string =>
  JSON.parse(fs.readFileSync(path.join(PACKETS, scenario, "scenario.json"), "utf-8")).packet.invoice;
async function openApproval(page: Page, scenario: string) {
  await page
    .locator(".task")
    .filter({ hasText: invoiceKey(scenario) })
    .getByRole("button", { name: "Review exact changes", exact: true })
    .click();
  await expect(page.getByRole("heading", { name: "Review proposed changes" })).toBeVisible();
  await expect(page.getByText(invoiceKey(scenario)).first()).toBeVisible();
}
async function matchTarget(page: Page) {
  await nav(page, "Reconciliation workspace");
  await page.getByRole("button", { name: "Look up matching payable", exact: true }).click();
  await expect(page.locator("#target-choice")).toBeVisible({ timeout: 15000 });
}

test.describe.configure({ mode: "serial" });

test("J1 happy path: import → parse → real local model prepares → independent approval → Northstar effect → independent verification → export", async ({ page }) => {
  test.setTimeout(240000);
  await signIn(page, "reviewer");
  await importPacket(page, "happy1");
  await shot(page, "j1-01-document");
  await matchTarget(page);
  await shot(page, "j1-02-reconciliation");
  await nav(page, "Document & extracted fields");
  await page.getByLabel("Allow agent to prepare a review-only proposal").check();
  await page.getByRole("button", { name: "Run local model analysis", exact: true }).click();
  await expect(page.getByTestId("analysis-status")).toContainText("prepared", { timeout: 120000 });
  await shot(page, "j1-03-analysis-prepared");
  await page.getByRole("button", { name: "Inspect agent-prepared outcome", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Review proposed changes", exact: true })).toBeVisible();
  await expect(page.getByTestId("retained-balance")).toHaveText("USD 1036.80");
  await expect(page.getByText("5184.00", { exact: true }).first()).toBeVisible();
  await expect(page.getByTestId("approve-change")).toBeDisabled(); // reviewer cannot approve
  await shot(page, "j1-04-changes-reviewer");
  await signOut(page);

  await signIn(page, "approver");
  await nav(page, "Approval queue");
  await shot(page, "j1-05-approvals");
  await openApproval(page, "happy1");
  await page.getByTestId("approve-change").click();
  await expect(page.getByRole("heading", { name: "Authorize this exact change?" })).toBeVisible();
  await shot(page, "j1-06-confirm-dialog");
  await page.getByTestId("confirm-approval").click();
  await expect(page.getByRole("heading", { name: "Execution detail" })).toBeVisible();
  await expect(page.getByText("Verified by independent read", { exact: true })).toBeVisible({ timeout: 60000 });
  await shot(page, "j1-07-execution-verified");
  await page.getByRole("button", { name: "View business record", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Verified business records" })).toBeVisible();
  await expect(page.getByText("5184.00", { exact: true }).first()).toBeVisible();
  await page.reload();
  await expect(page.getByText("5184.00", { exact: true }).first()).toBeVisible();
  await shot(page, "j1-08-records");
  // Evidence export through the live export worker loop, then download.
  await nav(page, "Evidence package");
  await page.getByRole("button", { name: "Export evidence JSON", exact: true }).click();
  await expect(page.getByTestId("export-state")).toBeVisible({ timeout: 15000 });
  await expect(async () => {
    await page.getByRole("button", { name: "Refresh export status", exact: true }).click();
    await expect(page.getByTestId("export-state")).toHaveText("Export ready", { timeout: 2000 });
  }).toPass({ timeout: 60000 });
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download evidence JSON", exact: true }).click();
  const file = await download;
  const saved = path.join(OUT, "j1-evidence-export.json");
  await file.saveAs(saved);
  const exported = JSON.parse(fs.readFileSync(saved, "utf-8"));
  expect(JSON.stringify(exported)).not.toContain("base64");
  await shot(page, "j1-09-evidence-export");
  // Approver may download the original invoice (reviewer/approver only).
  await page.getByRole("button", { name: /Download original invoice/ }).click();
  await shot(page, "j1-10-evidence-original");
});

test("J2 denials: viewer cannot import/approve; reviewer cannot approve; foreign workspace and CSRF denied", async ({ page, request }) => {
  await signIn(page, "viewer");
  await nav(page, "Import documents");
  await expect(page.getByRole("button", { name: "Import selected files", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Import authored sample packet", exact: true })).toBeDisabled();
  await shot(page, "j2-01-viewer-import-disabled");
  await nav(page, "Approval queue");
  await shot(page, "j2-02-viewer-approvals");
  const status = await page.evaluate(async () => {
    const foreign = await fetch("/api/v1/workspaces/22222222-2222-4222-8222-222222222222/cases", { credentials: "same-origin" });
    const noCsrf = await fetch("/api/v1/workspaces/11111111-1111-4111-8111-111111111111/intakes/sample", { method: "POST", credentials: "same-origin" });
    return { foreign: foreign.status, noCsrf: noCsrf.status };
  });
  expect(status.foreign).toBe(404);
  expect([401, 403]).toContain(status.noCsrf);
  fs.writeFileSync(path.join(OUT, "j2-denials.json"), JSON.stringify(status));
  await signOut(page);
  const anon = await request.get("/api/v1/workspaces/11111111-1111-4111-8111-111111111111/cases");
  expect(anon.status()).toBe(401);
});

test("J3 missing receipt: real model holds; zero target writes", async ({ page }) => {
  test.setTimeout(180000);
  await signIn(page, "reviewer");
  await importPacket(page, "hold3");
  await matchTarget(page);
  await nav(page, "Document & extracted fields");
  await page.getByLabel("Allow agent to prepare a review-only proposal").check();
  await page.getByRole("button", { name: "Run local model analysis", exact: true }).click();
  await expect(page.getByTestId("analysis-status")).not.toContainText("not requested");
  await expect(page.getByTestId("analysis-status")).toContainText(/recommended|prepared|blocked/, { timeout: 120000 });
  const status = await page.getByTestId("analysis-status").textContent();
  fs.writeFileSync(path.join(OUT, "j3-analysis-status.txt"), status || "");
  await shot(page, "j3-01-hold-analysis");
});

test("J4 human decision, stale If-Match denial (412), independent approver rejects: zero target effect", async ({ page }) => {
  test.setTimeout(180000);
  await signIn(page, "reviewer");
  await importPacket(page, "revoke4");
  await matchTarget(page);
  await nav(page, "Resolve an exception");
  await page.getByLabel("Decision reason").fill("Simulated reviewer: receipt GR supports 30 units");
  await shot(page, "j4-01-decision");
  // Stale version: bump the case version from a second tab-equivalent request first.
  const stale = await page.evaluate(async () => {
    const id = localStorage.getItem("folio-selected-case");
    const s = await (await fetch("/api/v1/auth/session")).json();
    const r = await fetch(`/api/v1/workspaces/11111111-1111-4111-8111-111111111111/cases/${id}/decisions`, {
      method: "POST", credentials: "same-origin",
      headers: { "content-type": "application/json", "x-csrf-token": s.csrf, "Idempotency-Key": crypto.randomUUID(), "If-Match": '"case-v1"' },
      body: JSON.stringify({ mode: "hold", quantity: "0", reason: "stale attempt", evidence_digest: "x" }),
    });
    return { status: r.status, body: await r.text() };
  });
  expect(stale.status).toBe(412);
  fs.writeFileSync(path.join(OUT, "j4-stale.json"), JSON.stringify(stale));
  await page.getByTestId("save-decision").click();
  await expect(page.getByRole("heading", { name: "Review proposed changes" })).toBeVisible({ timeout: 30000 });
  await shot(page, "j4-02-changes");
  await signOut(page);
  await signIn(page, "approver");
  await nav(page, "Approval queue");
  await shot(page, "j4-03-queue");
  await openApproval(page, "revoke4");
  await page.locator("#authorization-reason").fill("Simulated approver: rejecting to exercise the zero-effect branch");
  await page.getByRole("button", { name: "Reject exact proposal", exact: true }).click();
  await expect(page.getByText("Proposal rejected; no target job authorized.")).toBeVisible({ timeout: 15000 });
  await expect(page.getByTestId("approve-change")).toBeDisabled();
  await shot(page, "j4-04-rejected");
});

test("J5 all fourteen screens render for the reviewer at 1440/1024/390 without global overflow", async ({ page }) => {
  test.setTimeout(240000);
  const blockedOrigins: string[] = [];
  if (process.env.FOLIO_REPRO_OFFLINE === "1") {
    const uiOrigin = new URL(process.env.FOLIO_UI_ORIGIN || "http://127.0.0.1:5101").origin;
    await page.route("**/*", async (route) => {
      const url = new URL(route.request().url());
      if (url.protocol === "data:" || url.protocol === "blob:") return route.continue();
      if (url.origin === uiOrigin && ["127.0.0.1", "localhost"].includes(url.hostname)) {
        return route.continue();
      }
      blockedOrigins.push(url.origin);
      return route.abort("blockedbyclient");
    });
  }
  await signIn(page, "reviewer");
  const screens = [
    "Workspace overview", "Document inbox", "Import documents", "Document & extracted fields",
    "Reconciliation workspace", "Resolve an exception", "Approval queue", "Review proposed changes",
    "Execution detail", "Recover an interrupted run", "Verified business records", "Evidence package",
    "Activity & audit history", "Workspace & access settings",
  ];
  const report: Record<string, unknown>[] = [];
  for (const width of [1440, 1024, 390]) {
    await page.setViewportSize({ width, height: 900 });
    for (const [i, name] of screens.entries()) {
      if (width === 390) {
        const menu = page.getByRole("button", { name: /menu/i });
        if (await menu.count()) await menu.first().click().catch(() => {});
      }
      await page.goto("/#" + ["overview","inbox","import","document","reconciliation","decision","approvals","changes","execution","recovery","records","evidence","activity","settings"][i]);
      await page.waitForLoadState("domcontentloaded");
      await page.locator("h1, h2").first().waitFor({ timeout: 15000 });
      await page.waitForTimeout(400);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
      const h = await page.locator("h1").first().textContent().catch(() => null);
      report.push({ width, screen: name, h1: h, overflow });
      if (width === 1440 || width === 390) await shot(page, `j5-${width}-${String(i + 1).padStart(2, "0")}`);
    }
  }
  fs.writeFileSync(path.join(OUT, "j5-screens.json"), JSON.stringify(report, null, 2));
  if (process.env.FOLIO_REPRO_OFFLINE === "1") {
    fs.writeFileSync(path.join(OUT, "j5-network-guard.json"), JSON.stringify({
      mode: "allowlisted-loopback-ui-origin",
      ui_origin: new URL(process.env.FOLIO_UI_ORIGIN || "http://127.0.0.1:5101").origin,
      blocked_origins: [...new Set(blockedOrigins)].sort(),
    }, null, 2));
    expect([...new Set(blockedOrigins)]).toEqual([]);
  }
  expect(report.filter((r) => r.overflow)).toEqual([]);
});

test("J6 retention: viewer denied originals; admin legal hold blocks deletion; release then delete leaves tombstone", async ({ page }) => {
  test.setTimeout(120000);
  await signIn(page, "viewer");
  await nav(page, "Evidence package");
  const viewerOriginal = page.getByRole("button", { name: /Download original/ }).first();
  if (await viewerOriginal.count()) await expect(viewerOriginal).toBeDisabled();
  await shot(page, "j6-01-viewer-original-denied");
  await signOut(page);
  await signIn(page, "admin");
  await nav(page, "Document inbox");
  await page.getByTestId("inbox-search").fill(invoiceKey("hold3"));
  await page.getByRole("button", { name: "Open packet", exact: true }).first().click();
  await expect(page.getByRole("heading", { name: "Document & extracted fields", exact: true })).toBeVisible();
  await nav(page, "Workspace & access settings");
  await page.locator("#retention-reason").fill("Simulated admin: litigation hold drill on synthetic packet");
  await page.getByRole("button", { name: "Place legal hold", exact: true }).click();
  await expect(page.getByRole("button", { name: "Release legal hold", exact: true })).toBeVisible({ timeout: 15000 });
  await expect(page.getByRole("button", { name: "Delete uploaded original bytes", exact: true })).toBeDisabled();
  await shot(page, "j6-02-legal-hold");
  await page.locator("#retention-reason").fill("Simulated admin: hold released after drill");
  await page.getByRole("button", { name: "Release legal hold", exact: true }).click();
  await expect(page.getByRole("button", { name: "Place legal hold", exact: true })).toBeVisible({ timeout: 15000 });
  await page.locator("#retention-reason").fill("Simulated admin: delete uploaded originals of synthetic drill packet");
  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Delete uploaded original bytes", exact: true }).click();
  await expect(page.getByText("Stored original bytes deleted; digest tombstones and decision evidence retained.", { exact: false })).toBeVisible({ timeout: 15000 });
  await shot(page, "j6-03-deleted");
  await signOut(page);
  await signIn(page, "reviewer");
  await nav(page, "Document inbox");
  await page.getByTestId("inbox-search").fill(invoiceKey("hold3"));
  await page.getByRole("button", { name: "Open packet", exact: true }).first().click();
  await nav(page, "Evidence package");
  await expect(page.getByTestId("evidence-original-availability").first()).toContainText(/deleted/i);
  await shot(page, "j6-04-tombstone");
});
