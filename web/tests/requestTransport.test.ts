import assert from "node:assert/strict";
import test, { beforeEach } from "node:test";
import {
  createRequestTransport,
  MutationOutcomeUnknownError,
  PendingMutationConflictError,
  PendingMutationExpiredError,
  MutationDurabilityError,
} from "../src/requestTransport.ts";

const defaultStorageValues = new Map<string, string>();
Object.defineProperty(globalThis, "window", {
  configurable: true,
  value: {
    localStorage: {
      getItem: (key: string) => defaultStorageValues.get(key) ?? null,
      setItem: (key: string, value: string) => defaultStorageValues.set(key, value),
      removeItem: (key: string) => defaultStorageValues.delete(key),
    },
  },
});
beforeEach(() => defaultStorageValues.clear());

const principal = {
  actor: "reviewer@example.test",
  workspace: "11111111-1111-4111-8111-111111111111",
  csrf: "csrf-a",
};

function jsonResponse(status: number, data: unknown) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function captured(input: RequestInfo | URL, init?: RequestInit) {
  return {
    url: String(input),
    method: init?.method,
    headers: new Headers(init?.headers),
    body: init?.body,
  };
}

test("lost response replays the exact path, body, key, and If-Match", async () => {
  const requests: ReturnType<typeof captured>[] = [];
  let calls = 0;
  const transport = createRequestTransport({
    makeKey: () => "11111111-1111-4111-8111-111111111111",
    fetchImpl: async (input, init) => {
      requests.push(captured(input, init));
      if (++calls === 1) throw new TypeError("socket closed after send");
      return jsonResponse(200, { saved: true });
    },
  });
  const body = { reason: "duplicate receipt", typed_value: "120.00" };

  await assert.rejects(
    transport.request(
      "/api/v1/workspaces/111/cases/c1/fields/invoice",
      "PATCH",
      body,
      '"case-v7"',
      principal,
    ),
    MutationOutcomeUnknownError,
  );
  assert.deepEqual(
    await transport.retryPending({ ...principal, csrf: "csrf-refreshed" }),
    { saved: true },
  );
  assert.equal(requests.length, 2);
  assert.equal(requests[0].url, requests[1].url);
  assert.equal(requests[0].method, requests[1].method);
  assert.equal(requests[0].body, requests[1].body);
  assert.equal(requests[0].headers.get("Idempotency-Key"), requests[1].headers.get("Idempotency-Key"));
  assert.equal(requests[0].headers.get("If-Match"), '"case-v7"');
  assert.equal(requests[1].headers.get("If-Match"), '"case-v7"');
  assert.equal(requests[1].headers.get("X-CSRF-Token"), "csrf-refreshed");
  assert.equal(transport.pendingSummary(), null);
});

test("5xx and unreadable success bodies stay pending for exact replay", async () => {
  const requests: ReturnType<typeof captured>[] = [];
  let calls = 0;
  const transport = createRequestTransport({
    makeKey: () => "22222222-2222-4222-8222-222222222222",
    fetchImpl: async (input, init) => {
      requests.push(captured(input, init));
      if (++calls === 1) return new Response("not-json", { status: 503 });
      if (calls === 2) return new Response("not-json", { status: 200 });
      return jsonResponse(200, { receipt_id: "receipt-1" });
    },
  });
  const args = ["/api/v1/workspaces/111/cases/c1/decisions", "POST", { mode: "hold" }, '"case-v2"', principal] as const;

  await assert.rejects(transport.request(...args), MutationOutcomeUnknownError);
  await assert.rejects(transport.retryPending(principal), MutationOutcomeUnknownError);
  assert.deepEqual(await transport.retryPending(principal), { receipt_id: "receipt-1" });
  assert.equal(requests.length, 3);
  assert.deepEqual(requests.map((r) => r.headers.get("Idempotency-Key")), [
    "22222222-2222-4222-8222-222222222222",
    "22222222-2222-4222-8222-222222222222",
    "22222222-2222-4222-8222-222222222222",
  ]);
  assert.equal(new Set(requests.map((r) => r.body)).size, 1);
  assert.equal(new Set(requests.map((r) => r.headers.get("If-Match"))).size, 1);
});

test("different mutations are blocked while an outcome is unknown", async () => {
  let calls = 0;
  const transport = createRequestTransport({
    makeKey: () => "33333333-3333-4333-8333-333333333333",
    fetchImpl: async () => {
      calls += 1;
      throw new TypeError("offline");
    },
  });

  await assert.rejects(
    transport.request("/api/v1/workspaces/111/cases/c1/decisions", "POST", { mode: "hold" }, undefined, principal),
    MutationOutcomeUnknownError,
  );
  await assert.rejects(
    transport.request("/api/v1/workspaces/111/cases/c1/decisions", "POST", { mode: "partial" }, undefined, principal),
    PendingMutationConflictError,
  );
  assert.equal(calls, 1);
});

test("manual reconciliation can release only the original principal's pending command", async () => {
  let calls = 0;
  let key = 0;
  const keys: string[] = [];
  const transport = createRequestTransport({
    makeKey: () => `key-${++key}`,
    fetchImpl: async (_input, init) => {
      keys.push(new Headers(init?.headers).get("Idempotency-Key") || "");
      calls += 1;
      throw new TypeError("offline");
    },
  });
  await assert.rejects(
    transport.request("/api/v1/workspaces/111/cases/c1/decisions", "POST", { mode: "hold" }, undefined, principal),
    MutationOutcomeUnknownError,
  );
  assert.equal(
    transport.clearAfterExplicitReconciliation({ ...principal, actor: "other@example.test" }),
    false,
  );
  assert.equal(transport.clearAfterExplicitReconciliation(principal), true);
  assert.equal(transport.pendingSummary(), null);
  await assert.rejects(
    transport.request("/api/v1/workspaces/111/cases/c1/decisions", "POST", { mode: "hold" }, undefined, principal),
    MutationOutcomeUnknownError,
  );
  assert.equal(calls, 2);
  assert.deepEqual(keys, ["key-1", "key-2"]);
});

test("local abandon forgets only the saved retry and permits a new principal", async () => {
  let key = 0;
  let calls = 0;
  const requests: ReturnType<typeof captured>[] = [];
  const transport = createRequestTransport({
    makeKey: () => `key-${++key}`,
    fetchImpl: async (input, init) => {
      requests.push(captured(input, init));
      calls += 1;
      if (calls === 1) throw new TypeError("response lost");
      return jsonResponse(200, { accepted: true });
    },
  });
  await assert.rejects(
    transport.request(
      "/api/v1/workspaces/111/cases/c1/decisions",
      "POST",
      { mode: "hold", reason: "may have committed" },
      '"case-v4"',
      principal,
    ),
    MutationOutcomeUnknownError,
  );

  const summary = transport.pendingSummary();
  assert.ok(summary);
  assert.equal(JSON.stringify(summary).includes("may have committed"), false);
  await assert.rejects(
    transport.request(
      "/api/v1/workspaces/111/cases/c1/decisions",
      "POST",
      { mode: "hold", reason: "may have committed" },
      '"case-v4"',
      { ...principal, actor: "other@example.test" },
    ),
    PendingMutationConflictError,
  );
  assert.equal(calls, 1);
  assert.equal(transport.abandonPendingLocally(), true);
  assert.equal(transport.pendingSummary(), null);
  assert.equal(transport.hasUnresolvedMutation(), false);
  assert.equal(transport.canAttemptLoginAs("other@example.test"), true);
  assert.deepEqual(
    await transport.request(
      "/api/v1/workspaces/222/cases/c9/decisions",
      "POST",
      { mode: "hold", reason: "new tenant action" },
      '"case-v2"',
      { ...principal, actor: "other@example.test", workspace: "22222222-2222-4222-8222-222222222222" },
    ),
    { accepted: true },
  );
  assert.equal(calls, 2);
  assert.deepEqual(requests.map((request) => request.headers.get("Idempotency-Key")), [
    "key-1",
    "key-2",
  ]);
  assert.equal(requests[0].body, '{"mode":"hold","reason":"may have committed"}');
  assert.equal(requests[1].body, '{"mode":"hold","reason":"new tenant action"}');
});

test("definite 4xx resolves the attempt so a later same-payload command gets a new key", async () => {
  const requests: ReturnType<typeof captured>[] = [];
  let calls = 0;
  const transport = createRequestTransport({
    makeKey: (() => {
      let index = 0;
      return () => `key-${++index}`;
    })(),
    fetchImpl: async (input, init) => {
      requests.push(captured(input, init));
      if (++calls === 1) return jsonResponse(409, { detail: "stale version" });
      return jsonResponse(200, { accepted: true });
    },
  });
  const args = ["/api/v1/workspaces/111/cases/c1/decisions", "POST", { mode: "hold" }, '"case-v2"', principal] as const;

  await assert.rejects(transport.request(...args), /stale version/);
  assert.equal(transport.pendingSummary(), null);
  assert.deepEqual(await transport.request(...args), { accepted: true });
  assert.deepEqual(requests.map((r) => r.headers.get("Idempotency-Key")), ["key-1", "key-2"]);
});

test("auth reset preserves replay for the original principal; expiry requires reconciliation", async () => {
  let time = 1000;
  let key = 0;
  let calls = 0;
  const transport = createRequestTransport({
    now: () => time,
    ttlMs: 10,
    makeKey: () => `key-${++key}`,
    fetchImpl: async () => {
      calls += 1;
      if (calls === 1) throw new TypeError("offline");
      return jsonResponse(200, { accepted: true });
    },
  });
  await assert.rejects(
    transport.request("/api/v1/workspaces/111/cases/c1/decisions", "POST", {}, undefined, principal),
    MutationOutcomeUnknownError,
  );

  await assert.rejects(
    transport.retryPending({ ...principal, actor: "approver@example.test" }),
    PendingMutationConflictError,
  );
  assert.equal(transport.canAttemptLoginAs("approver@example.test"), false);
  assert.equal(transport.canAttemptLoginAs(principal.actor), true);
  assert.deepEqual(await transport.retryPending({ ...principal, csrf: "csrf-new" }), { accepted: true });
  assert.equal(calls, 2);

  const expired = createRequestTransport({
    now: () => time,
    ttlMs: 10,
    makeKey: () => `key-${++key}`,
    fetchImpl: async () => {
      throw new TypeError("offline");
    },
  });
  await assert.rejects(
    expired.request("/api/v1/workspaces/111/cases/c1/decisions", "POST", {}, undefined, principal),
    MutationOutcomeUnknownError,
  );
  time += 10;
  assert.equal(expired.needsReconciliation(), true);
  await assert.rejects(
    expired.request("/api/v1/workspaces/111/cases/c1/decisions", "POST", {}, undefined, principal),
    PendingMutationExpiredError,
  );
  assert.equal(
    expired.clearAfterExplicitReconciliation({ ...principal, actor: "approver@example.test" }),
    false,
  );
  assert.equal(expired.clearAfterExplicitReconciliation(principal), true);
  assert.equal(expired.needsReconciliation(), false);
});

test("reload retains a route hash, never a route/body, and checks the exact receipt", async () => {
  const values = new Map<string, string>();
  const storage = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  };
  const transport = createRequestTransport({
    storage,
    makeKey: () => "44444444-4444-4444-8444-444444444444",
    fetchImpl: async () => { throw new TypeError("lost response"); },
  });
  await assert.rejects(
    transport.request("/api/v1/workspaces/111/cases/c1/decisions", "POST", { secret: "do-not-persist" }, '"case-v3"', principal),
    MutationOutcomeUnknownError,
  );
  const persisted = [...values.values()].join(" ");
  assert.match(persisted, /44444444-4444-4444-8444-444444444444/);
  assert.match(persisted, /reviewer@example.test/);
  assert.doesNotMatch(persisted, /secret|case-v3|csrf|decisions|c1/);
  const expectedDigest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode("POST\n/api/v1/workspaces/111/cases/c1/decisions"),
  );
  const expectedHash = [...new Uint8Array(expectedDigest)]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
  assert.match(persisted, new RegExp(expectedHash));

  const lookups: string[] = [];
  const reloaded = createRequestTransport({
    storage,
    fetchImpl: async (input) => {
      lookups.push(String(input));
      return jsonResponse(200, { status: "committed" });
    },
  });
  assert.equal(reloaded.pendingSummary()?.durableOnly, true);
  await assert.rejects(
    reloaded.request("/api/v1/workspaces/111/cases/c2/decisions", "POST", {}, undefined, principal),
    PendingMutationConflictError,
  );
  await assert.rejects(
    reloaded.lookupDurableOutcome({ ...principal, actor: "approver@example.test" }),
    PendingMutationConflictError,
  );
  assert.equal(await reloaded.lookupDurableOutcome(principal), "committed");
  assert.equal(lookups.length, 1);
  assert.match(lookups[0], /44444444-4444-4444-8444-444444444444/);
  assert.match(lookups[0], new RegExp(`operation_hash=${expectedHash}`));
  assert.equal(reloaded.hasUnresolvedMutation(), false);
  assert.equal(values.size, 0);
});

test("a legacy route-less blocker cannot be cleared by an unrelated same-key receipt", async () => {
  const values = new Map<string, string>([["folio-unknown-mutation-v1", JSON.stringify({
    actor: principal.actor,
    workspace: principal.workspace,
    idempotencyKey: "88888888-8888-4888-8888-888888888888",
    createdAt: 1,
    expiresAt: Date.now() + 60_000,
    logout: false,
  })]]);
  let calls = 0;
  const transport = createRequestTransport({
    storage: { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: (key) => values.delete(key) },
    fetchImpl: async () => { calls += 1; return jsonResponse(200, { status: "committed" }); },
  });
  assert.equal(await transport.lookupDurableOutcome(principal), null);
  assert.equal(calls, 0);
  assert.equal(transport.hasUnresolvedMutation(), true);
});

test("a retried logout 401 remains unresolved until its receipt is checked", async () => {
  const transport = createRequestTransport({
    makeKey: () => "55555555-5555-4555-8555-555555555555",
    fetchImpl: async () => jsonResponse(401, { detail: "SESSION_REQUIRED" }),
  });
  await assert.rejects(
    transport.request("/api/v1/auth/logout", "POST", {}, undefined, principal),
    MutationOutcomeUnknownError,
  );
  assert.equal(transport.pendingSummary()?.logout, true);
  assert.equal(transport.hasUnresolvedMutation(), true);
});

test("authorization loss during an exact retry preserves the unknown outcome", async () => {
  let calls = 0;
  const transport = createRequestTransport({
    makeKey: () => "66666666-6666-4666-8666-666666666666",
    fetchImpl: async () => {
      calls += 1;
      return calls === 1
        ? Promise.reject(new TypeError("response lost"))
        : jsonResponse(403, { detail: "PERMISSION_DENIED" });
    },
  });
  const args = ["/api/v1/workspaces/111/cases/c1/decisions", "POST", { mode: "hold" }, undefined, principal] as const;
  await assert.rejects(transport.request(...args), MutationOutcomeUnknownError);
  await assert.rejects(transport.retryPending(principal), MutationOutcomeUnknownError);
  assert.equal(transport.pendingSummary()?.durableOnly, false);
  assert.equal(transport.hasUnresolvedMutation(), true);
});

test("a missing server receipt does not clear the reload blocker", async () => {
  const values = new Map<string, string>();
  const storage = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  };
  const first = createRequestTransport({
    storage,
    makeKey: () => "77777777-7777-4777-8777-777777777777",
    fetchImpl: async () => { throw new TypeError("lost response"); },
  });
  await assert.rejects(
    first.request("/api/v1/workspaces/111/cases/c1/decisions", "POST", { mode: "hold" }, undefined, principal),
    MutationOutcomeUnknownError,
  );
  const reloaded = createRequestTransport({
    storage,
    fetchImpl: async () => jsonResponse(200, { status: "not_recorded" }),
  });
  assert.equal(await reloaded.lookupDurableOutcome(principal), "not_recorded");
  assert.equal(reloaded.hasUnresolvedMutation(), true);
  assert.equal(reloaded.pendingSummary()?.durableOnly, true);
  assert.equal(values.size, 1);
});

test("durable intent is written before the first POST and contains only scoped opaque identifiers", async () => {
  const values = new Map<string, string>();
  const order: string[] = [];
  const storage = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { order.push("persist"); values.set(key, value); },
    removeItem: (key: string) => values.delete(key),
  };
  const transport = createRequestTransport({
    storage,
    makeKey: () => "99999999-9999-4999-8999-999999999999",
    fetchImpl: async () => { order.push("post"); throw new TypeError("lost response"); },
  });
  await assert.rejects(
    transport.request("/api/v1/workspaces/111/cases/c1/decisions", "POST", { secret: "private-body" }, '"case-v3"', principal),
    MutationOutcomeUnknownError,
  );
  assert.deepEqual(order.slice(0, 2), ["persist", "post"]);
  const persisted = [...values.values()].join(" ");
  assert.match(persisted, /reviewer@example\.test/);
  assert.match(persisted, /11111111-1111-4111-8111-111111111111/);
  assert.match(persisted, /99999999-9999-4999-8999-999999999999/);
  assert.doesNotMatch(persisted, /private-body|case-v3|csrf|decisions|c1/);
});

test("storage write failure prevents any command POST", async () => {
  let calls = 0;
  const transport = createRequestTransport({
    storage: {
      getItem: () => null,
      setItem: () => { throw new Error("quota exceeded"); },
      removeItem: () => undefined,
    },
    fetchImpl: async () => { calls += 1; return jsonResponse(200, { ok: true }); },
  });
  await assert.rejects(
    transport.request("/api/v1/workspaces/111/cases/c1/decisions", "POST", { mode: "hold" }, undefined, principal),
    MutationDurabilityError,
  );
  assert.equal(calls, 0);
});

test("unreadable and corrupt durable state fail closed across reload", async () => {
  for (const storage of [
    { getItem: () => { throw new Error("blocked"); }, setItem: () => undefined, removeItem: () => undefined },
    { getItem: () => "{not-json", setItem: () => undefined, removeItem: () => undefined },
    { getItem: () => "", setItem: () => undefined, removeItem: () => undefined },
  ]) {
    let calls = 0;
    const transport = createRequestTransport({
      storage,
      fetchImpl: async () => { calls += 1; return jsonResponse(200, { ok: true }); },
    });
    assert.notEqual(transport.durabilityProblem(), "");
    assert.equal(transport.hasUnresolvedMutation(), true);
    await assert.rejects(
      transport.request("/api/v1/workspaces/111/cases/c1/decisions", "POST", {}, undefined, principal),
      MutationDurabilityError,
    );
    assert.equal(calls, 0);
  }
});

test("failed marker cleanup keeps the confirmed request blocked", async () => {
  const values = new Map<string, string>();
  const storage = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: () => { throw new Error("storage is read-only"); },
  };
  const transport = createRequestTransport({
    storage,
    fetchImpl: async () => jsonResponse(200, { saved: true }),
  });
  await assert.rejects(
    transport.request("/api/v1/workspaces/111/cases/c1/decisions", "POST", { mode: "hold" }, undefined, principal),
    MutationDurabilityError,
  );
  assert.equal(transport.hasUnresolvedMutation(), true);
  assert.equal(values.size, 1);
});

test("multipart intake uses browser framing, exact in-memory retry, and opaque durable state", async () => {
  const requests: ReturnType<typeof captured>[] = [];
  let calls = 0;
  const transport = createRequestTransport({
    makeKey: () => "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    fetchImpl: async (input, init) => {
      requests.push(captured(input, init));
      if (++calls === 1) throw new TypeError("response lost");
      return jsonResponse(200, { accepted: ["invoice.pdf"] });
    },
  });
  const form = new FormData();
  form.append("rights_attestation", "authored-synthetic");
  form.append("document_roles", "invoice");
  form.append("files", new Blob(["authored bytes"], { type: "application/pdf" }), "invoice.pdf");
  await assert.rejects(
    transport.request("/api/v1/workspaces/111/intakes/files", "POST", form, undefined, principal),
    MutationOutcomeUnknownError,
  );
  assert.equal(requests[0].headers.has("Content-Type"), false);
  assert.equal(requests[0].body, form);
  assert.deepEqual(
    await transport.retryPending(principal),
    { accepted: ["invoice.pdf"] },
  );
  assert.equal(requests[1].body, form);
  assert.equal(requests[1].headers.has("Content-Type"), false);
  assert.equal([...defaultStorageValues.values()].length, 0);
});

test("multipart transport cannot bypass limits on unrelated mutation routes", async () => {
  let calls = 0;
  const transport = createRequestTransport({
    fetchImpl: async () => { calls += 1; return jsonResponse(200, {}); },
  });
  await assert.rejects(
    transport.request("/api/v1/workspaces/111/decisions", "POST", new FormData(), undefined, principal),
    /limited to document intake/,
  );
  assert.equal(calls, 0);
});
