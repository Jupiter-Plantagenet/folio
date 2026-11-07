export type RequestPrincipal = {
  actor: string;
  workspace: string;
  csrf: string;
};

export type PendingMutationSummary = {
  createdAt: number;
  expiresAt: number;
  expired: boolean;
  logout: boolean;
  durableOnly: boolean;
  idempotencyKey: string;
};

type DurableBlocker = {
  actor: string;
  workspace: string;
  idempotencyKey: string;
  operationHash?: string;
  createdAt: number;
  expiresAt: number;
  logout: boolean;
};

type MutationRequest = {
  principalIdentity: string;
  actor: string;
  fingerprint: string;
  idempotencyKey: string;
  operationHash: string;
  method: string;
  path: string;
  bodyText?: string;
  formData?: FormData;
  version?: string;
  createdAt: number;
  expiresAt: number;
};

type TransportOptions = {
  fetchImpl?: typeof fetch;
  now?: () => number;
  makeKey?: () => string;
  ttlMs?: number;
  maxPendingBodyBytes?: number;
  storage?: Pick<Storage, "getItem" | "setItem" | "removeItem">;
};

type DurableBlockerRead = { blocker: DurableBlocker | null; problem: string };

const DEFAULT_TTL_MS = 365 * 24 * 60 * 60 * 1000;
const DEFAULT_MAX_PENDING_BODY_BYTES = 64 * 1024 * 1024;
const DURABLE_BLOCKER_KEY = "folio-unknown-mutation-v1";

export class MutationOutcomeUnknownError extends Error {
  constructor(message = "The mutation response was lost or unreadable.") {
    super(message);
    this.name = "MutationOutcomeUnknownError";
  }
}

export class PendingMutationConflictError extends Error {
  constructor() {
    super(
      "Another change is waiting for a confirmed response. Retry that exact request before starting a different change.",
    );
    this.name = "PendingMutationConflictError";
  }
}

export class PendingMutationExpiredError extends Error {
  constructor() {
    super(
      "The saved request is past the safe replay window. Reconcile the current workspace state before allowing another change.",
    );
    this.name = "PendingMutationExpiredError";
  }
}

export class MutationDurabilityError extends Error {
  constructor(message = "Folio cannot safely save the pending command in this browser. No change was sent. Restore browser storage access, then retry.") {
    super(message);
    this.name = "MutationDurabilityError";
  }
}

function responseMessage(data: unknown, fallback: string): string {
  if (typeof data === "object" && data !== null && "detail" in data) {
    const detail = (data as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (detail !== undefined) return JSON.stringify(detail);
  }
  return fallback;
}

function principalIdentity(principal: Pick<RequestPrincipal, "actor" | "workspace">): string {
  return `${principal.actor}\u0000${principal.workspace}`;
}

function requestFingerprint(
  principal: RequestPrincipal,
  method: string,
  path: string,
  bodyText?: string,
  version?: string,
): string {
  return JSON.stringify([
    principal.actor,
    principal.workspace,
    path,
    method,
    bodyText ?? null,
    version ?? null,
  ]);
}

function formDataFingerprint(formData: FormData): string {
  return JSON.stringify(
    [...formData.entries()].map(([name, value]) =>
      typeof value === "string"
        ? [name, value]
        : [name, value.name, value.size, value.type, value.lastModified],
    ),
  );
}

async function operationHash(method: string, path: string): Promise<string> {
  const bytes = new TextEncoder().encode(`${method.toUpperCase()}\n${path}`);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
}

export function createRequestTransport(options: TransportOptions = {}) {
  const fetchImpl = options.fetchImpl ?? fetch;
  const now = options.now ?? Date.now;
  const makeKey = options.makeKey ?? (() => crypto.randomUUID());
  const ttlMs = options.ttlMs ?? DEFAULT_TTL_MS;
  const maxPendingBodyBytes =
    options.maxPendingBodyBytes ?? DEFAULT_MAX_PENDING_BODY_BYTES;
  let pending: MutationRequest | null = null;
  const initialDurableState = readDurableBlocker(options.storage);
  let durableBlocker: DurableBlocker | null = initialDurableState.blocker;
  let storageProblem = initialDurableState.problem;
  let expiredPending = false;
  let expiredPrincipalIdentity: string | null = null;
  let expiredActor: string | null = null;

  function expireIfNeeded() {
    if (pending && now() >= pending.expiresAt) {
      expiredPrincipalIdentity = pending.principalIdentity;
      expiredActor = pending.actor;
      try { removeDurableBlocker(options.storage); }
      catch (error) { storageProblem = (error as Error).message; return; }
      pending = null;
      expiredPending = true;
    }
    if (durableBlocker && now() >= durableBlocker.expiresAt) {
      expiredPrincipalIdentity = principalIdentity(durableBlocker);
      expiredActor = durableBlocker.actor;
      try { removeDurableBlocker(options.storage); }
      catch (error) { storageProblem = (error as Error).message; return; }
      durableBlocker = null;
      expiredPending = true;
    }
  }

  function pendingSummary(): PendingMutationSummary | null {
    expireIfNeeded();
    if (!pending && !durableBlocker) return null;
    const blocker = pending ?? durableBlocker!;
    return {
      createdAt: blocker.createdAt,
      expiresAt: blocker.expiresAt,
      expired: now() >= blocker.expiresAt,
      logout: "path" in blocker ? blocker.path === "/api/v1/auth/logout" : blocker.logout,
      durableOnly: !pending,
      idempotencyKey: blocker.idempotencyKey,
    };
  }

  function needsReconciliation(): boolean {
    expireIfNeeded();
    return expiredPending;
  }

  function hasUnresolvedMutation(): boolean {
    expireIfNeeded();
    return pending !== null || durableBlocker !== null || expiredPending || !!storageProblem;
  }

  function durabilityProblem(): string {
    return storageProblem;
  }

  function canAttemptLoginAs(actor: string): boolean {
    if (pending) return pending.actor === actor;
    if (durableBlocker) return durableBlocker.actor === actor;
    return !expiredPending || expiredActor === actor;
  }

  function principalMatchesPending(principal: RequestPrincipal): boolean {
    const identity = principalIdentity(principal);
    if (pending) return pending.principalIdentity === identity;
    if (durableBlocker) return principalIdentity(durableBlocker) === identity;
    return !expiredPending || expiredPrincipalIdentity === identity;
  }

  function clearAfterExplicitReconciliation(
    principal: RequestPrincipal,
  ): boolean {
    const identity = principalIdentity(principal);
    if (pending) {
      if (pending.principalIdentity !== identity) return false;
      removeDurableBlocker(options.storage);
      pending = null;
      durableBlocker = null;
      storageProblem = "";
      expiredPending = false;
      expiredPrincipalIdentity = null;
      expiredActor = null;
      return true;
    }
    if (durableBlocker) {
      if (principalIdentity(durableBlocker) !== identity) return false;
      removeDurableBlocker(options.storage);
      durableBlocker = null;
      storageProblem = "";
      expiredPending = false;
      expiredPrincipalIdentity = null;
      expiredActor = null;
      return true;
    }
    if (
      !expiredPending ||
      expiredPrincipalIdentity !== identity
    )
      return false;
    removeDurableBlocker(options.storage);
    storageProblem = "";
    expiredPending = false;
    expiredPrincipalIdentity = null;
    expiredActor = null;
    return true;
  }

  function abandonPendingLocally(): boolean {
    if (!hasUnresolvedMutation()) return false;
    removeDurableBlocker(options.storage);
    pending = null;
    durableBlocker = null;
    storageProblem = "";
    expiredPending = false;
    expiredPrincipalIdentity = null;
    expiredActor = null;
    return true;
  }

  function savePending(request: MutationRequest) {
    const blocker: DurableBlocker = {
      actor: request.actor,
      workspace: request.principalIdentity.split("\u0000")[1] ?? "",
      idempotencyKey: request.idempotencyKey,
      operationHash: request.operationHash,
      createdAt: request.createdAt,
      expiresAt: request.expiresAt,
      logout: request.path === "/api/v1/auth/logout",
    };
    writeDurableBlocker(options.storage, blocker);
    pending = request;
    expiredPending = false;
    durableBlocker = blocker;
  }

  function clearPending(request: MutationRequest) {
    if (durableBlocker?.idempotencyKey === request.idempotencyKey) {
      removeDurableBlocker(options.storage);
      durableBlocker = null;
    }
    if (pending?.idempotencyKey === request.idempotencyKey) pending = null;
  }

  async function sendMutation(
    request: MutationRequest,
    principal: RequestPrincipal,
    replay = false,
  ) {
    const headers: Record<string, string> = {
      "X-CSRF-Token": principal.csrf,
      "Idempotency-Key": request.idempotencyKey,
    };
    if (!request.formData) headers["Content-Type"] = "application/json";
    if (request.version) headers["If-Match"] = request.version;

    let response: Response;
    try {
      response = await fetchImpl(request.path, {
        method: request.method,
        headers,
        body: request.formData ?? request.bodyText,
      });
    } catch {
      throw new MutationOutcomeUnknownError(
        "The connection ended before Folio received a response. The request may have completed.",
      );
    }

    if (!response.ok) {
      if (replay && (response.status === 401 || response.status === 403)) {
        throw new MutationOutcomeUnknownError(
          "This retry is no longer authorized. The earlier request may have completed before access changed; keep the blocker and reconcile it with the original account.",
        );
      }
      if (request.path === "/api/v1/auth/logout" && response.status === 401) {
        throw new MutationOutcomeUnknownError(
          "The logout session is no longer active. The earlier logout may have completed; sign in as the same account to check its server receipt.",
        );
      }
      if (response.status >= 500) {
        let message = "The server returned an error; the request may have completed.";
        try {
          message = responseMessage(await response.json(), message);
        } catch {
          // Keep the original request for an exact replay when a 5xx body is unreadable.
        }
        throw new MutationOutcomeUnknownError(message);
      }

      clearPending(request);
      let data: unknown = {};
      try {
        data = await response.json();
      } catch {
        // A completed 4xx response is a definite rejection even if its body is malformed.
      }
      throw new Error(responseMessage(data, `Request failed (${response.status}).`));
    }

    let data: unknown;
    try {
      data = await response.json();
    } catch {
      throw new MutationOutcomeUnknownError(
        "Folio accepted the request but its response could not be read. Its outcome is unknown until the exact request is retried.",
      );
    }

    clearPending(request);
    return data;
  }

  async function request(
    path: string,
    method = "GET",
    body?: unknown,
    version?: string,
    principal?: RequestPrincipal,
  ): Promise<any> {
    const verb = method.toUpperCase();
    if (verb === "GET" || verb === "HEAD") {
      const response = await fetchImpl(path, { method: verb });
      const data = await response.json();
      if (!response.ok)
        throw new Error(responseMessage(data, `Request failed (${response.status}).`));
      return data;
    }

    if (!principal?.actor || !principal.workspace)
      throw new Error("A signed-in workspace is required for this change.");
    if (storageProblem) throw new MutationDurabilityError(storageProblem);
    expireIfNeeded();
    if (expiredPending) throw new PendingMutationExpiredError();
    if (!pending && durableBlocker) throw new PendingMutationConflictError();

    const formData = typeof FormData !== "undefined" && body instanceof FormData
      ? body
      : undefined;
    if (formData && (verb !== "POST" || !path.endsWith("/intakes/files")))
      throw new Error("Multipart transport is limited to document intake.");
    const bodyText = formData ? undefined : body === undefined ? undefined : JSON.stringify(body);
    const bodyBytes = bodyText ? new TextEncoder().encode(bodyText).byteLength : 0;
    if (bodyBytes > maxPendingBodyBytes)
      throw new Error(
        "This request is too large for safe in-memory retry. No change was sent.",
      );
    const fingerprint = requestFingerprint(
      principal,
      verb,
      path,
      formData ? `multipart:${formDataFingerprint(formData)}` : bodyText,
      version,
    );

    if (pending) {
      if (
        pending.principalIdentity !== principalIdentity(principal) ||
        pending.fingerprint !== fingerprint
      )
        throw new PendingMutationConflictError();
      return sendMutation(pending, principal, true);
    }
    if (durableBlocker) throw new PendingMutationConflictError();

    const createdAt = now();
    const request: MutationRequest = {
      principalIdentity: principalIdentity(principal),
      actor: principal.actor,
      fingerprint,
      idempotencyKey: makeKey(),
      operationHash: await operationHash(verb, path),
      method: verb,
      path,
      bodyText,
      formData,
      version,
      createdAt,
      expiresAt: createdAt + ttlMs,
    };
    // Record only actor/workspace, the method+route hash and the opaque command
    // key. The body, raw route, CSRF token and cookie never enter browser storage.
    savePending(request);
    return sendMutation(request, principal);
  }

  async function retryPending(principal: RequestPrincipal): Promise<any> {
    expireIfNeeded();
    if (expiredPending) throw new PendingMutationExpiredError();
    if (!pending && durableBlocker) throw new PendingMutationConflictError();
    if (!pending) throw new Error("There is no unresolved request to retry.");
    if (pending.principalIdentity !== principalIdentity(principal))
      throw new PendingMutationConflictError();
    return sendMutation(pending, principal, true);
  }

  async function lookupDurableOutcome(
    principal: RequestPrincipal,
  ): Promise<"committed" | "not_recorded" | null> {
    expireIfNeeded();
    const blocker = durableBlocker ?? (pending ? {
      actor: pending.actor,
      workspace: pending.principalIdentity.split("\u0000")[1] ?? "",
      idempotencyKey: pending.idempotencyKey,
      operationHash: pending.operationHash,
      createdAt: pending.createdAt,
      expiresAt: pending.expiresAt,
      logout: pending.path === "/api/v1/auth/logout",
    } : null);
    if (!blocker) return null;
    if (principalIdentity(blocker) !== principalIdentity(principal))
      throw new PendingMutationConflictError();
    // Legacy v1 blockers have no route binding. They cannot establish an exact
    // receipt and remain unresolved until explicit audit reconciliation.
    if (!blocker.operationHash) return null;
    const response = await fetchImpl(
      `/api/v1/auth/mutation-receipts/${encodeURIComponent(blocker.idempotencyKey)}?operation_hash=${encodeURIComponent(blocker.operationHash)}`,
      { method: "GET", cache: "no-store" },
    );
    if (!response.ok)
      throw new Error("The server receipt lookup is unavailable. The request remains blocked.");
    const data = await response.json();
    if (data?.status === "committed") {
      removeDurableBlocker(options.storage);
      pending = null;
      durableBlocker = null;
      storageProblem = "";
      return "committed";
    }
    return data?.status === "not_recorded" ? "not_recorded" : null;
  }

  return {
    clearAfterExplicitReconciliation,
    abandonPendingLocally,
    canAttemptLoginAs,
    hasUnresolvedMutation,
    needsReconciliation,
    pendingSummary,
    principalMatchesPending,
    request,
    retryPending,
    lookupDurableOutcome,
    durabilityProblem,
  };
}

function readDurableBlocker(
  storage?: TransportOptions["storage"],
): DurableBlockerRead {
  try {
    const raw = (storage ?? window.localStorage).getItem(DURABLE_BLOCKER_KEY);
    if (raw === null) return { blocker: null, problem: "" };
    const value = JSON.parse(raw);
    if (
      typeof value?.actor === "string" && value.actor.length > 0 &&
      typeof value?.workspace === "string" && value.workspace.length > 0 &&
      typeof value?.idempotencyKey === "string" && value.idempotencyKey.length > 0 &&
      (value?.operationHash === undefined ||
        (typeof value.operationHash === "string" && /^[0-9a-f]{64}$/.test(value.operationHash))) &&
      Number.isFinite(value?.createdAt) &&
      Number.isFinite(value?.expiresAt) &&
      typeof value?.logout === "boolean"
    ) {
      // A malformed/newer route binding is treated like a route-less legacy
      // blocker: it remains unresolved and cannot produce a committed receipt.
      const safeHash =
        typeof value.operationHash === "string" && /^[0-9a-f]{64}$/.test(value.operationHash)
          ? value.operationHash
          : undefined;
      return {
        blocker: {
          actor: value.actor,
          workspace: value.workspace,
          idempotencyKey: value.idempotencyKey,
          operationHash: safeHash,
          createdAt: value.createdAt,
          expiresAt: value.expiresAt,
          logout: value.logout,
        },
        problem: "",
      };
    }
    return {
      blocker: null,
      problem: "Saved mutation state is unreadable or incomplete. No new changes can be sent. Inspect this workspace's authorized case and activity history; after resolving the earlier outcome, remove only the damaged `folio-unknown-mutation-v1` entry from this site's browser storage and reload.",
    };
  } catch {
    return {
      blocker: null,
      problem: "Browser storage is unavailable or unreadable. No new changes can be sent because Folio cannot check for an earlier unresolved command. Restore site storage access and reload; if a prior command may have run, reconcile its outcome first.",
    };
  }
}

function writeDurableBlocker(
  storage: TransportOptions["storage"] | undefined,
  value: DurableBlocker,
) {
  try {
    (storage ?? window.localStorage).setItem(DURABLE_BLOCKER_KEY, JSON.stringify(value));
  } catch {
    throw new MutationDurabilityError();
  }
}

function removeDurableBlocker(storage?: TransportOptions["storage"]) {
  try { (storage ?? window.localStorage).removeItem(DURABLE_BLOCKER_KEY); }
  catch {
    throw new MutationDurabilityError("Folio could not clear its saved command marker. New commands remain blocked; restore browser storage access, then reconcile the saved server receipt before continuing.");
  }
}
