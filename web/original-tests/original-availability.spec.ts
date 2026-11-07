import { test, expect, Page } from "@playwright/test";
// Authored stubs only: no database, retained API, private identity or provider.
const workspace = "00000000-0000-4000-8000-000000000001";
const cid = "00000000-0000-4000-8000-000000000002";
const prefix = `/api/v1/workspaces/${workspace}`;
const sid = (n: number) =>
  `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const attachedBytes = "Receipt,GR-A\nLine,1\nReceived quantity,40\n";
const states = [
  ["attached.csv", "available"],
  ["invoice.pdf", "not_stored"],
  ["deleted.csv", "deleted"],
  ["tampered.csv", "digest_mismatch"],
  ["lost.csv", "missing"],
  ["empty.csv", "bytes_missing"],
  ["huge.csv", "too_large"],
  ["ambiguous.csv", "metadata_mismatch"],
] as const;
const codes: Record<string, string | null> = {
  available: null,
  not_stored: "NOT_FOUND",
  missing: "NOT_FOUND",
  deleted: "ORIGINAL_DELETED",
  digest_mismatch: "ORIGINAL_DIGEST_MISMATCH",
  bytes_missing: "ORIGINAL_BYTES_MISSING",
  too_large: "ORIGINAL_TOO_LARGE",
  metadata_mismatch: "ORIGINAL_METADATA_MISMATCH",
};
const sources = states.map(([name], i) => ({
  id: sid(i + 1),
  name,
  sha256: String(i).repeat(64).slice(0, 64),
  version: 1,
  pages: [
    {
      number: 1,
      text: "Synthetic",
      text_sha256: "c".repeat(64),
      words: [{ quote: "Synthetic", start: 0, end: 9, boxes: [] }],
    },
  ],
}));
function availability(override: Record<string, string> = {}) {
  return {
    schema_version: "folio-original-availability-v1",
    advisory: true,
    download_permitted: true,
    legal_hold: false,
    sources: states.map(([, state], i) => {
      const actual = override[sid(i + 1)] || state;
      return {
        source_id: sid(i + 1),
        source_version: 1,
        state: actual,
        code: codes[actual],
        downloadable: actual === "available",
        bytes_stored: [
          "available",
          "digest_mismatch",
          "too_large",
          "metadata_mismatch",
        ].includes(actual),
        retention_expired: actual === "available" ? false : null,
      };
    }),
  };
}
type Options = {
  roles?: string[];
  report?: unknown; // undefined => old API that omits the field
  downloadStatus?: number;
};
async function setup(page: Page, options: Options) {
  const downloads: string[] = [];
  const unexpected: string[] = [];
  let report = options.report;
  const downloadStatus = options.downloadStatus ?? 200;
  let detailReads = 0;
  await page.route("**/api/**", async (route) => {
    const req = route.request(),
      url = new URL(req.url());
    const send = (data: unknown, status = 200) =>
      route.fulfill({ status, json: data });
    if (url.pathname === "/api/v1/auth/session")
      return send({
        username: "ui-authored-reviewer",
        roles: options.roles || ["reviewer"],
        csrf: "authored-csrf",
        workspace,
      });
    const c: Record<string, unknown> = {
      id: cid,
      label: "UI-AUTHORED",
      invoice: "UI-ONLY",
      supplier: "Authored Supplier",
      version: 3,
      state: "needs_review",
      sources,
      source_digest: "a".repeat(64),
      // Sample-origin packet with attached support: no intake marker at all.
      sample_digest: "fixture",
      proposal: null,
      verification: null,
      received_quantity: "1",
      retention: {
        legal_hold: false,
        original_days: 90,
        decision_evidence_days: 365,
      },
    };
    if (report !== undefined) c.original_availability = report;
    if (url.pathname === `${prefix}/cases`) return send({ items: [c] });
    if (url.pathname === `${prefix}/cases/${cid}`) {
      detailReads += 1;
      return send(c);
    }
    if (url.pathname === `${prefix}/events`)
      return route.fulfill({
        contentType: "text/event-stream",
        body: ": isolated stub\n\n",
      });
    const original = url.pathname.match(
      new RegExp(`^${prefix}/cases/${cid}/sources/([0-9a-f-]+)/original$`),
    );
    if (original) {
      downloads.push(original[1]);
      if (downloadStatus !== 200) {
        // Bytes changed after the advisory report; the next detail read says so.
        report = availability({ [original[1]]: "digest_mismatch" });
        return send({ detail: "ORIGINAL_DIGEST_MISMATCH" }, downloadStatus);
      }
      return route.fulfill({
        body: attachedBytes,
        contentType: "application/octet-stream",
        headers: {
          "Content-Disposition": `attachment; filename="source-${original[1]}"`,
          "Cache-Control": "no-store",
        },
      });
    }
    unexpected.push(req.method() + " " + url.pathname);
    return route.abort(); // Never proxy an unmatched request to a retained backend.
  });
  return {
    downloads,
    unexpected,
    setReport: (value: unknown) => {
      report = value;
    },
    detailReads: () => detailReads,
  };
}
const evidenceButton = (page: Page, name: string) =>
  page.getByRole("button", { name: `Download original ${name}`, exact: true });

test("evidence offers only the integrity-checked attached original on a sample-origin case", async ({
  page,
}) => {
  const fixture = await setup(page, { report: availability() });
  await page.goto("/#evidence");
  await expect(
    page.getByRole("heading", { name: "Immutable evidence inventory" }),
  ).toBeVisible();
  await expect(evidenceButton(page, "attached.csv")).toBeEnabled();
  const expectations: Record<string, RegExp> = {
    not_stored: /Original not stored.*authored sample fixture/,
    deleted: /Original bytes deleted.*digest tombstone remains/,
    digest_mismatch:
      /Integrity check failed.*do not match the recorded SHA-256/,
    missing: /Original missing.*no stored original object/,
    bytes_missing: /Original bytes missing.*no deletion tombstone/,
    too_large: /Original too large.*20 MiB/,
    metadata_mismatch: /Original unavailable.*exactly one stored original/,
  };
  const notes = page.getByTestId("evidence-original-availability");
  await expect(notes).toHaveCount(states.length);
  for (const [i, [name, state]] of states.entries()) {
    await expect(notes.nth(i)).toHaveAttribute("data-state", state);
    if (state === "available") {
      await expect(notes.nth(i)).toContainText("re-hashes the bytes at download");
      continue;
    }
    await expect(evidenceButton(page, name)).toBeDisabled();
    await expect(notes.nth(i)).toContainText(expectations[state]);
    await expect(evidenceButton(page, name)).toHaveAccessibleDescription(
      expectations[state],
    );
  }
  const downloadEvent = page.waitForEvent("download");
  await evidenceButton(page, "attached.csv").click();
  const download = await downloadEvent;
  expect(download.suggestedFilename()).toBe("attached.csv");
  expect(fixture.downloads).toEqual([sid(1)]);
  expect(fixture.unexpected).toEqual([]);
});

test("document screen follows per-source availability, not the intake marker", async ({
  page,
}) => {
  const fixture = await setup(page, { report: availability() });
  await page.goto("/#document");
  const control = page.getByTestId("document-original-download");
  await expect(control).toHaveText("Download uploaded original");
  await expect(control).toBeEnabled();
  await page.getByRole("button", { name: "invoice.pdf", exact: true }).click();
  await expect(control).toHaveText("Original not stored");
  await expect(control).toBeDisabled();
  await expect(control).toHaveAccessibleDescription(/authored sample fixture/);
  await page.getByRole("button", { name: "tampered.csv", exact: true }).click();
  await expect(control).toHaveText("Integrity check failed");
  await expect(control).toBeDisabled();
  await page.getByRole("button", { name: "deleted.csv", exact: true }).click();
  await expect(control).toHaveText("Original bytes deleted");
  await expect(control).toBeDisabled();
  expect(fixture.downloads).toEqual([]);
  expect(fixture.unexpected).toEqual([]);
});

test("old API without availability withholds every original download and deletion", async ({
  page,
}) => {
  const fixture = await setup(page, { roles: ["admin"], report: undefined });
  await page.goto("/#evidence");
  for (const [name] of states) {
    await expect(evidenceButton(page, name)).toBeDisabled();
    await expect(evidenceButton(page, name)).toHaveAccessibleDescription(
      /did not report per-source original availability/,
    );
  }
  await page.goto("/#document");
  await expect(page.getByTestId("document-original-download")).toHaveText(
    "Original availability unknown",
  );
  await expect(page.getByTestId("document-original-download")).toBeDisabled();
  await page.goto("/#settings");
  await page
    .getByLabel("Retention action reason")
    .fill("Owner requested deletion");
  await expect(
    page.getByRole("button", { name: "Delete uploaded original bytes" }),
  ).toBeDisabled();
  await expect(
    page.getByText(/availability was not reported by this server/),
  ).toBeVisible();
  // An unrecognized schema is treated the same as an omitted field.
  fixture.setReport({ ...availability(), schema_version: "future-v9" });
  await page.goto("/#evidence");
  await page.reload();
  await expect(evidenceButton(page, "attached.csv")).toBeDisabled();
  expect(fixture.downloads).toEqual([]);
  expect(fixture.unexpected).toEqual([]);
});

test("a role without original-export permission is never offered a download", async ({
  page,
}) => {
  const fixture = await setup(page, {
    roles: ["viewer"],
    report: { ...availability(), download_permitted: false },
  });
  await page.goto("/#evidence");
  for (const [name] of states) {
    await expect(evidenceButton(page, name)).toBeDisabled();
    await expect(evidenceButton(page, name)).toHaveAccessibleDescription(
      /Your role cannot download original files/,
    );
  }
  await page.goto("/#document");
  await expect(page.getByTestId("document-original-download")).toHaveText(
    "Original download not permitted",
  );
  await expect(page.getByTestId("document-original-download")).toBeDisabled();
  expect(fixture.downloads).toEqual([]);
  expect(fixture.unexpected).toEqual([]);
});

test("stored originals on a sample-origin case enable authorized deletion controls", async ({
  page,
}) => {
  const fixture = await setup(page, {
    roles: ["admin"],
    report: availability(),
  });
  await page.goto("/#settings");
  await expect(
    page.getByText("4 source(s) currently have stored original bytes."),
  ).toBeVisible();
  const remove = page.getByRole("button", {
    name: "Delete uploaded original bytes",
  });
  await expect(remove).toBeDisabled();
  await page
    .getByLabel("Retention action reason")
    .fill("Owner requested deletion");
  await expect(remove).toBeEnabled();
  expect(fixture.unexpected).toEqual([]);
});

test("download-time integrity denial is explained and refreshes advisory state", async ({
  page,
}) => {
  const fixture = await setup(page, {
    report: availability(),
    downloadStatus: 503,
  });
  await page.goto("/#evidence");
  await expect(evidenceButton(page, "attached.csv")).toBeEnabled();
  const before = fixture.detailReads();
  await evidenceButton(page, "attached.csv").click();
  await expect(page.getByRole("alert")).toContainText(
    "failed their SHA-256 check at download. Nothing was downloaded.",
  );
  expect(fixture.detailReads()).toBeGreaterThan(before);
  await expect(evidenceButton(page, "attached.csv")).toBeDisabled();
  expect(fixture.downloads).toEqual([sid(1)]);
  expect(fixture.unexpected).toEqual([]);
});
