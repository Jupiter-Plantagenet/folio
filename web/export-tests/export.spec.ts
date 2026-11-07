import { test, expect, Page } from "@playwright/test";
import { createHash } from "node:crypto";
import fs from "node:fs/promises";
import AxeBuilder from "@axe-core/playwright";
const workspace = "00000000-0000-4000-8000-000000000001";
const cid = "00000000-0000-4000-8000-000000000002";
const job = "00000000-0000-4000-8000-000000000003";
const prefix = `/api/v1/workspaces/${workspace}`;
const artifactPath = `${prefix}/artifacts/${job}`;
// Authored metadata only. No database, private identity, provider or retained service.
const manifest = {
  schema_version: "folio-evidence-v2",
  redaction_profile: "audit-v1",
  scope: "case-evidence-metadata-originals-excluded",
  release_ready: false,
  case: {
    id: cid,
    version: 7,
    snapshot_sha256: "a".repeat(64),
    source_digest: "b".repeat(64),
  },
  sources: [],
  field_provenance: {},
  proposal: null,
  verification: {
    status: "incomplete",
    snapshot_sha256: null,
    version: null,
    verifier_version_sha256: createHash("sha256").update("null").digest("hex"),
  },
  omissions: [
    "originals",
    "source text and filenames",
    "business field values and quotes",
    "private paths",
    "provider secrets",
    "grants",
    "auth headers and cookies",
    "proposal effect payload",
    "verifier observation payload",
  ],
  limitations: [
    "Metadata-only audit subset; no new verification or model claim",
    "Not a full release or full retention/backup/provider-deletion proof",
  ],
};
const raw = JSON.stringify(manifest);
const digest = createHash("sha256").update(raw).digest("hex");
async function setup(page: Page, roles = ["reviewer"]) {
  const commands: { body: any; headers: Record<string, string> }[] = [];
  const unexpected: string[] = [];
  let state = "queued",
    denial = 0,
    dropEnqueue = false,
    revision = 7;
  await page.route("**/api/**", async (route) => {
    const req = route.request(),
      url = new URL(req.url());
    const send = (data: unknown, status = 200) =>
      route.fulfill({ status, json: data });
    if (url.pathname === "/api/v1/auth/session")
      return send({
        username: "ui-authored-reviewer",
        roles,
        csrf: "authored-csrf",
        workspace,
      });
    const c = {
      id: cid,
      label: "UI-AUTHORED",
      invoice: "UI-ONLY",
      supplier: "Authored Supplier",
      version: revision,
      state: "needs_review",
      sources: [],
      source_digest: "a".repeat(64),
      sample_digest: "fixture",
      proposal: null,
      verification: null,
      received_quantity: "1",
    };
    if (url.pathname === `${prefix}/cases`) return send({ items: [c] });
    if (url.pathname === `${prefix}/cases/${cid}`) return send(c);
    if (url.pathname === `${prefix}/events`)
      return route.fulfill({
        contentType: "text/event-stream",
        body: ": isolated stub\n\n",
      });
    if (
      url.pathname === `${prefix}/cases/${cid}/exports` &&
      req.method() === "POST"
    ) {
      commands.push({ body: req.postDataJSON(), headers: req.headers() });
      if (dropEnqueue) {
        dropEnqueue = false;
        return route.abort();
      }
      return send({ artifact_job_id: job }, 202);
    }
    if (url.pathname === artifactPath) {
      if (denial)
        return send(
          { detail: denial === 401 ? "SESSION_REQUIRED" : "EXPORT_DENIED" },
          denial,
        );
      if (url.search === "?download=true")
        return route.fulfill({
          contentType: "application/json",
          body: raw,
          headers: {
            ETag: `"sha256-${digest}"`,
            "Content-Disposition": `attachment; filename="folio-evidence-${job}.json"`,
          },
        });
      return send({
        artifact_job_id: job,
        state,
        manifest,
        snapshot_sha256: digest,
        sha256: state === "ready" ? digest : null,
        bytes: state === "ready" ? Buffer.byteLength(raw) : null,
        download_url: artifactPath + "?download=true",
      });
    }
    unexpected.push(req.method() + " " + url.pathname);
    return route.abort(); // Never proxy an unmatched request to the retained backend.
  });
  await page.goto("/#evidence");
  await expect(
    page.getByRole("heading", { name: "Immutable evidence inventory" }),
  ).toBeVisible();
  return {
    commands,
    unexpected,
    setVersion: (value: number) => { revision = value; },
    drop: () => {
      dropEnqueue = true;
    },
    ready: () => {
      state = "ready";
    },
    deny: (status: number) => {
      denial = status;
    },
  };
}
test("enqueue metadata-only job, refresh queued status, download exact authorized bytes", async ({
  page,
}) => {
  const fixture = await setup(page);
  await page
    .getByRole("button", { name: "Export evidence JSON", exact: true })
    .click();
  await expect(page.getByTestId("export-state")).toHaveText("Export queued");
  expect(fixture.commands).toHaveLength(1);
  expect(fixture.commands[0].body).toEqual({
    case_version: 7,
    include_originals: false,
    redaction_profile: "audit-v1",
  });
  expect(fixture.commands[0].headers["x-csrf-token"]).toBe("authored-csrf");
  expect(fixture.commands[0].headers["if-match"]).toBe('"case-v7"');
  expect(fixture.commands[0].headers["idempotency-key"]).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
  );
  await expect(
    page.getByRole("button", { name: "Download evidence JSON", exact: true }),
  ).toBeDisabled();
  fixture.ready();
  await page.getByRole("button", { name: "Refresh export status" }).click();
  await expect(page.getByTestId("export-state")).toHaveText("Export ready");
  await expect(page.getByLabel("Export manifest")).toContainText("incomplete");
  await expect(page.getByLabel("Export manifest")).toContainText(
    "release_ready",
  );
  const downloadEvent = page.waitForEvent("download");
  await page
    .getByRole("button", { name: "Download evidence JSON", exact: true })
    .click();
  const download = await downloadEvent;
  expect(download.suggestedFilename()).toBe(`folio-evidence-${job}.json`);
  expect(await fs.readFile((await download.path())!, "utf8")).toBe(raw);
  await expect(
    page.getByRole("status").filter({ hasText: "Evidence JSON downloaded" }),
  ).toBeVisible();
  expect(fixture.unexpected).toEqual([]);
});

for (const [code, message] of [
  [401, "Session ended. Sign in again; no artifact was downloaded."],
  [403, "Export access denied. Current permissions do not allow this request."],
  [404, "Export unavailable for this account and workspace."],
  [
    410,
    "Export expired or deleted. Previously issued URLs no longer grant access.",
  ],
  [409, "Artifact is not ready. Refresh export status."],
  [503, "Export service unavailable. Retry status; do not create another job."],
] as const) {
  test(`current authorization/download denial ${code} clears ready authority`, async ({
    page,
  }) => {
    const fixture = await setup(page);
    fixture.ready();
    await page
      .getByRole("button", { name: "Export evidence JSON", exact: true })
      .click();
    await expect(page.getByTestId("export-state")).toHaveText("Export ready");
    const downloads: string[] = [];
    page.on("download", (d) => downloads.push(d.suggestedFilename()));
    fixture.deny(code);
    await page
      .getByRole("button", { name: "Download evidence JSON", exact: true })
      .click();
    await expect(page.getByRole("alert")).toHaveText(message);
    await expect(
      page.getByRole("button", { name: "Download evidence JSON", exact: true }),
    ).toBeDisabled();
    expect(downloads).toEqual([]);
    expect(fixture.commands).toHaveLength(1);
    expect(fixture.unexpected).toEqual([]);
  });
}
test("viewer cannot enqueue an export", async ({ page }) => {
  const fixture = await setup(page, ["viewer"]);
  await expect(
    page.getByRole("button", { name: "Export evidence JSON", exact: true }),
  ).toBeDisabled();
  expect(fixture.commands).toEqual([]);
});

test("lost enqueue response retries exact frozen command after browser refresh", async ({
  page,
}) => {
  const fixture = await setup(page);
  fixture.drop();
  await page
    .getByRole("button", { name: "Export evidence JSON", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "Submission outcome unknown",
  );
  await page.reload();
  await page.getByRole("button", { name: "Retry same export request" }).click();
  await expect(page.getByTestId("export-state")).toHaveText("Export queued");
  expect(fixture.commands).toHaveLength(2);
  expect(fixture.commands[1]).toEqual(fixture.commands[0]);
  await page.reload();
  await expect(page.getByTestId("export-state")).toHaveText("Export queued");
  expect(fixture.commands).toHaveLength(2);
  expect(
    await page.evaluate(() => JSON.stringify(sessionStorage)),
  ).not.toContain("authored-csrf");
});

test("leaving evidence screen aborts an in-flight download before local publication", async ({
  page,
}) => {
  const fixture = await setup(page);
  fixture.ready();
  await page
    .getByRole("button", { name: "Export evidence JSON", exact: true })
    .click();
  await expect(page.getByTestId("export-state")).toHaveText("Export ready");
  let release!: () => void;
  const barrier = new Promise<void>((resolve) => {
    release = resolve;
  });
  let entered!: () => void;
  const requested = new Promise<void>((resolve) => {
    entered = resolve;
  });
  await page.route("**/artifacts/*?download=true", async (route) => {
    entered();
    await barrier;
    await route
      .fulfill({
        contentType: "application/json",
        body: raw,
        headers: { ETag: `"sha256-${digest}"` },
      })
      .catch(() => {});
  });
  const downloads: string[] = [];
  page.on("download", (d) => downloads.push(d.suggestedFilename()));
  await page
    .getByRole("button", { name: "Download evidence JSON", exact: true })
    .click();
  await requested;
  await page.evaluate(() => {
    location.hash = "document";
  });
  await expect(page.locator("main")).toHaveAttribute(
    "data-current-screen",
    "document",
  );
  release();
  // Bounded post-release observation; not a physical backend race proof.
  await page.waitForTimeout(250);
  expect(downloads).toEqual([]);
});

for (const [code, detail, message] of [
  [
    412,
    "STALE_VERSION",
    "Case changed. Original request identity retained. Refresh the case for inspection; ask an operator to reconcile the original command before any new export.",
  ],
  [
    422,
    "REDACTION_INVALID",
    "Export parameters were rejected. Only metadata-only audit-v1 is supported.",
  ],
  [
    409,
    "RETENTION_DELETED",
    "Decision evidence was deleted. This case cannot be exported.",
  ],
] as const) {
  test(`enqueue ${detail} is an honest rejection, never a download`, async ({
    page,
  }) => {
    const fixture = await setup(page);
    await page.route("**/cases/*/exports", (route) =>
      route.fulfill({ status: code, json: { detail } }),
    );
    await page
      .getByRole("button", { name: "Export evidence JSON", exact: true })
      .click();
    await expect(page.getByRole("alert")).toHaveText(message);
    await expect(page.getByTestId("export-state")).toHaveCount(0);
    expect(fixture.commands).toEqual([]);
  });
}

// Regression checks for guards added with the tracer bullets above.
const frozenKey = "00000000-0000-4000-8000-000000000010";
const recoveryKey = JSON.stringify(["folio-export-v1", "ui-authored-reviewer", workspace, cid]);
const frozen = { key: frozenKey, version: 7, job: "" };

test("storage read failure blocks fresh POST, explicit reread retries original key/body/If-Match", async ({ page }) => {
  const fixture = await setup(page);
  fixture.drop();
  await page.getByRole("button", { name: "Export evidence JSON", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Submission outcome unknown");
  expect(fixture.commands).toHaveLength(1);
  const original = fixture.commands[0];
  await page.addInitScript(storageKey => {
    const get = Storage.prototype.getItem;
    let fail = true;
    Storage.prototype.getItem = function (key) {
      if (key === storageKey && fail) { fail = false; throw new Error("authored read failure"); }
      return get.call(this, key);
    };
  }, recoveryKey);
  fixture.setVersion(9);
  await page.reload();
  await expect(page.getByRole("alert")).toContainText("Creation is blocked");
  await expect(page.getByRole("button", { name: "Export evidence JSON", exact: true })).toBeDisabled();
  expect(fixture.commands).toHaveLength(1);
  await page.getByRole("button", { name: "Retry recovery read" }).click();
  expect(fixture.commands).toHaveLength(1);
  await page.getByRole("button", { name: "Retry same export request" }).click();
  await expect(page.getByTestId("export-state")).toHaveText("Export queued");
  expect(fixture.commands).toHaveLength(2);
  expect(fixture.commands[1]).toEqual(original);
  expect(fixture.commands[1].headers["if-match"]).toBe('"case-v7"');
  expect(fixture.commands[1].body).toEqual({ case_version: 7, include_originals: false, redaction_profile: "audit-v1" });
});

for (const [label, value] of [
  ["malformed JSON", "{"], ["null", "null"], ["array root", "[]"],
  ["array key", JSON.stringify({ ...frozen, key: [frozenKey] })],
  ["array job", JSON.stringify({ ...frozen, job: [job] })],
  ["string revision", JSON.stringify({ ...frozen, version: "7" })],
  ["zero revision", JSON.stringify({ ...frozen, version: 0 })],
  ["unsafe revision", JSON.stringify({ ...frozen, version: Number.MAX_SAFE_INTEGER + 1 })],
  ["null job", JSON.stringify({ ...frozen, job: null })],
] as const) {
  test(`invalid stored ${label} stays blocked without POST or storage replacement`, async ({ page }) => {
    await page.addInitScript(({ key, value }) => sessionStorage.setItem(key, value), { key: recoveryKey, value });
    const fixture = await setup(page);
    await expect(page.getByRole("alert")).toContainText("Creation is blocked");
    await page.getByRole("button", { name: "Retry recovery read" }).click();
    await expect(page.getByRole("button", { name: "Export evidence JSON", exact: true })).toBeDisabled();
    expect(fixture.commands).toHaveLength(0);
    expect(await page.evaluate(key => sessionStorage.getItem(key), recoveryKey)).toBe(value);
  });
}
test("extra persisted fields are stripped before retry and acknowledgement writes", async ({ page }) => {
  await page.addInitScript(({ key, value }) => {
    sessionStorage.setItem(key, value);
    const set = Storage.prototype.setItem;
    (window as any).exportWrites = [];
    Storage.prototype.setItem = function (k, v) {
      if (k === key) (window as any).exportWrites.push(JSON.parse(v));
      return set.call(this, k, v);
    };
  }, { key: recoveryKey, value: JSON.stringify({ ...frozen, private: "AUTHORED_EXTRA" }) });
  const fixture = await setup(page);
  await page.getByRole("button", { name: "Retry same export request" }).click();
  await expect(page.getByTestId("export-state")).toHaveText("Export queued");
  const writes = await page.evaluate(() => (window as any).exportWrites);
  expect(writes).toHaveLength(2);
  for (const value of writes) expect(Object.keys(value).sort()).toEqual(["job", "key", "version"]);
  expect(fixture.commands[0].headers["idempotency-key"]).toBe(frozenKey);
});

test("array acknowledgement cannot drive status or replace original command", async ({ page }) => {
  const fixture = await setup(page);
  let posts = 0;
  await page.route("**/cases/*/exports", route => { posts++; return route.fulfill({ status: 202, json: { artifact_job_id: [job] } }); });
  await page.getByRole("button", { name: "Export evidence JSON", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Invalid export job identity");
  const first = await page.evaluate(key => sessionStorage.getItem(key), recoveryKey);
  await page.getByRole("button", { name: "Retry same export request" }).click();
  await expect(page.getByRole("alert")).toContainText("Invalid export job identity");
  expect(await page.evaluate(key => sessionStorage.getItem(key), recoveryKey)).toBe(first);
  expect(posts).toBe(2);
  expect(fixture.unexpected).toEqual([]);
  await expect(page.getByTestId("export-state")).toHaveCount(0);
});

for (const [code, detail, guidance] of [
  [409, "IDEMPOTENCY_DIGEST_MISMATCH", "identity conflicts with an earlier command"],
  [422, "EXPORT_INPUT_TOO_LARGE", "Export exceeds metadata size limits"],
  [422, "EXPORT_TOO_LARGE", "Export exceeds metadata size limits"],
  [503, "EXPORT_UNAVAILABLE", "Submission outcome unknown. Retry the same export request"],
] as const) {
  test(`POST ${code}/${detail} retains exact replay and operation-specific guidance`, async ({ page }) => {
    const fixture = await setup(page);
    const posts: { body: string | null; key: string; match: string }[] = [];
    await page.route("**/cases/*/exports", route => {
      const req = route.request();
      posts.push({ body: req.postData(), key: req.headers()["idempotency-key"], match: req.headers()["if-match"] });
      return route.fulfill({ status: code, json: { detail } });
    });
    await page.getByRole("button", { name: "Export evidence JSON", exact: true }).click();
    await expect(page.getByRole("alert")).toContainText(guidance);
    fixture.setVersion(9);
    await page.reload();
    await page.getByRole("button", { name: "Retry same export request" }).click();
    await expect(page.getByRole("alert")).toContainText(guidance);
    expect(posts).toHaveLength(2);
    expect(posts[1]).toEqual(posts[0]);
    expect(posts[1].match).toBe('"case-v7"');
    await expect(page.getByTestId("export-state")).toHaveCount(0);
  });
}

// Real browser ReadableStream API; authored fetch response, not a live HTTP chunking proof.
for (const scenario of ["declared oversize", "chunked oversize", "lying small length", "single oversize chunk", "valid chunked"] as const) {
  test(`bounded stream ${scenario}: cancel before hashing/publication on limit`, async ({ page }) => {
    const fixture = await setup(page); fixture.ready();
    await page.getByRole("button", { name: "Export evidence JSON", exact: true }).click();
    await expect(page.getByTestId("export-state")).toHaveText("Export ready");
    await page.evaluate(({ scenario, raw, digest }) => {
      const original = window.fetch.bind(window);
      const metrics = { reads: 0, cancels: 0, digests: 0, publications: 0, arrayBuffers: 0 };
      (window as any).streamMetrics = metrics;
      const hash = crypto.subtle.digest.bind(crypto.subtle);
      crypto.subtle.digest = ((...args: any[]) => { metrics.digests++; return (hash as any)(...args); }) as any;
      const publish = URL.createObjectURL.bind(URL);
      URL.createObjectURL = object => { metrics.publications++; return publish(object); };
      window.fetch = async (input, init) => {
        if (!String(input).endsWith("?download=true")) return original(input, init);
        const bytes = new TextEncoder().encode(raw);
        const chunks = scenario === "valid chunked" ? [bytes.slice(0, 10), bytes.slice(10)] : scenario === "single oversize chunk" ? [new Uint8Array(262145)] : [new Uint8Array(131072), new Uint8Array(131072), new Uint8Array(1)];
        const stream = new ReadableStream<Uint8Array>({ pull(controller) { const chunk = chunks.shift(); if (chunk) controller.enqueue(chunk); else controller.close(); }, cancel() { metrics.cancels++; } }, { highWaterMark: 0 });
        const headers: Record<string, string> = { ETag: `"sha256-${digest}"` };
        if (scenario === "declared oversize") headers["Content-Length"] = "262145";
        if (scenario === "lying small length") headers["Content-Length"] = "1";
        const response = new Response(stream, { headers });
        const get = response.body!.getReader.bind(response.body);
        response.body!.getReader = (() => { const reader = get(); const read = reader.read.bind(reader); reader.read = () => { metrics.reads++; return read(); }; return reader; }) as any;
        response.arrayBuffer = async () => { metrics.arrayBuffers++; throw new Error("unbounded materialization forbidden"); };
        return response;
      };
    }, { scenario, raw, digest });
    const downloads: string[] = []; page.on("download", d => downloads.push(d.suggestedFilename()));
    await page.getByRole("button", { name: "Download evidence JSON", exact: true }).click();
    if (scenario === "valid chunked") await expect(page.getByRole("status").filter({ hasText: "Evidence JSON downloaded" })).toBeVisible();
    else await expect(page.getByRole("alert")).toContainText("integrity check failed");
    const metrics = await page.evaluate(() => (window as any).streamMetrics);
    expect(metrics.arrayBuffers).toBe(0);
    expect(metrics.digests).toBe(scenario === "valid chunked" ? 1 : 0);
    expect(metrics.publications).toBe(scenario === "valid chunked" ? 1 : 0);
    expect(metrics.cancels).toBe(scenario === "valid chunked" ? 0 : 1);
    expect(metrics.reads).toBe(scenario === "declared oversize" ? 0 : scenario === "single oversize chunk" ? 1 : 3);
    expect(downloads).toHaveLength(scenario === "valid chunked" ? 1 : 0);
  });
}

for (const change of ["username", "workspace", "caseId", "unmount"] as const) {
  test(`late acknowledgement after ${change} cannot persist job or start status`, async ({ page }) => {
    let enter!: () => void, release!: () => void;
    const entered = new Promise<void>(r => enter = r), barrier = new Promise<void>(r => release = r);
    const calls: string[] = [];
    await page.route("**/api/**", async route => {
      calls.push(route.request().method() + " " + new URL(route.request().url()).pathname);
      if (route.request().method() === "POST") { enter(); await barrier; await route.fulfill({ status: 202, json: { artifact_job_id: job } }).catch(() => {}); }
      else await route.abort();
    });
    await page.goto("/export-tests/identity-fixture.html");
    await page.getByRole("button", { name: "Export evidence JSON", exact: true }).click();
    await entered;
    const saved = await page.evaluate(key => sessionStorage.getItem(key), recoveryKey);
    await page.evaluate(change => {
      if (change === "unmount") (window as any).unmountExport();
      else (window as any).mountExport({ [change]: change === "username" ? "another-authored-account" : "00000000-0000-4000-8000-000000000005" });
    }, change);
    release();
    await page.waitForTimeout(250); // bounded late-callback observation, not backend race evidence
    expect(calls).toHaveLength(1);
    expect(await page.evaluate(key => sessionStorage.getItem(key), recoveryKey)).toBe(saved);
    await expect(page.getByTestId("export-state")).toHaveCount(0);
    if (change !== "unmount") await expect(page.getByRole("button", { name: "Export evidence JSON", exact: true })).toBeEnabled();
  });
}

test("tampered artifact never becomes a local download", async ({ page }) => {
  const fixture = await setup(page);
  fixture.ready();
  await page
    .getByRole("button", { name: "Export evidence JSON", exact: true })
    .click();
  await expect(page.getByTestId("export-state")).toHaveText("Export ready");
  await page.route("**/artifacts/*?download=true", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: '{"tampered":true}',
      headers: { ETag: `"sha256-${digest}"` },
    }),
  );
  const downloads: string[] = [];
  page.on("download", (d) => downloads.push(d.suggestedFilename()));
  await page
    .getByRole("button", { name: "Download evidence JSON", exact: true })
    .click();
  await expect(page.getByRole("alert")).toHaveText(
    "Export integrity check failed. Nothing downloaded.",
  );
  expect(downloads).toEqual([]);
});
for (const roles of [["approver"], ["auditor"]]) {
  test(`${roles[0]} can submit metadata-only export`, async ({ page }) => {
    const fixture = await setup(page, roles);
    await page
      .getByRole("button", { name: "Export evidence JSON", exact: true })
      .click();
    await expect(page.getByTestId("export-state")).toHaveText("Export queued");
    expect(fixture.commands).toHaveLength(1);
  });
}
test("reviewer to viewer revokes submission but preserves authorized actor artifact read", async ({
  page,
}) => {
  const roles = ["reviewer"];
  const fixture = await setup(page, roles);
  fixture.ready();
  await page
    .getByRole("button", { name: "Export evidence JSON", exact: true })
    .click();
  await expect(page.getByTestId("export-state")).toHaveText("Export ready");
  roles.splice(0, 1, "viewer");
  await page.reload();
  await expect(page.getByTestId("export-state")).toHaveText("Export ready");
  await expect(
    page.getByRole("button", { name: "Export evidence JSON", exact: true }),
  ).toBeDisabled();
  const download = page.waitForEvent("download");
  await page
    .getByRole("button", { name: "Download evidence JSON", exact: true })
    .click();
  await download;
  expect(fixture.commands).toHaveLength(1);
});
test("evidence export remains accessible without global overflow at 390/1024/1440px", async ({
  page,
}) => {
  const fixture = await setup(page);
  fixture.ready();
  await page
    .getByRole("button", { name: "Export evidence JSON", exact: true })
    .click();
  await expect(page.getByTestId("export-state")).toHaveText("Export ready");
  for (const width of [390, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth > innerWidth,
      ),
    ).toBe(false);
    await page.screenshot({
      path: `${process.env.FOLIO_EXPORT_UI_EVIDENCE}/evidence-${width}.png`,
      fullPage: true,
    });
  }
  const axe = await new AxeBuilder({ page })
    .include("main")
    .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
    .analyze();
  await fs.writeFile(
    `${process.env.FOLIO_EXPORT_UI_EVIDENCE}/axe.json`,
    JSON.stringify(axe, null, 2),
  );
  expect(
    axe.violations.filter((v) =>
      ["serious", "critical"].includes(v.impact || ""),
    ),
  ).toEqual([]);
});
