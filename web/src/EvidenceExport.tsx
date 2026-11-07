import React, { useEffect, useRef, useState } from "react";

type Pending = { key: string; version: number; job: string };
type Props = {
  username: string;
  workspace: string;
  caseId: string;
  version: number;
  roles: string[];
  csrf: string;
};
type Artifact = {
  artifact_job_id: string;
  state: string;
  manifest: unknown;
  sha256: string | null;
  bytes: number | null;
};
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export function EvidenceExport({
  username,
  workspace,
  caseId,
  version,
  roles,
  csrf,
}: Props) {
  const storageKey = JSON.stringify([
    "folio-export-v1",
    username,
    workspace,
    caseId,
  ]);
  function restore(): { pending: Pending | null; blocked: boolean } {
    try {
      const stored = sessionStorage.getItem(storageKey);
      if (stored === null) return { pending: null, blocked: false };
      const value = JSON.parse(stored);
      if (
        value !== null &&
        typeof value === "object" &&
        !Array.isArray(value) &&
        typeof value.key === "string" &&
        uuid.test(value.key) &&
        Number.isSafeInteger(value.version) &&
        value.version > 0 &&
        typeof value.job === "string" &&
        (value.job === "" || uuid.test(value.job))
      ) return {
        pending: { key: value.key, version: value.version, job: value.job },
        blocked: false,
      };
    } catch {
      // Unknown is not absence: a command may already have committed.
    }
    return { pending: null, blocked: true };
  }
  const [recovery, setRecovery] = useState(restore);
  const pending = recovery.pending;
  const setPending = (value: Pending | null) =>
    setRecovery({ pending: value, blocked: false });
  const job = pending?.job || "";
  function remember(value: Pending) {
    // Persist only command identifiers/revision, never evidence, session or CSRF.
    // Fail before submitting if storage is unavailable: do not lose replay identity.
    sessionStorage.setItem(storageKey, JSON.stringify(value));
    setPending(value);
  }
  useEffect(() => {
    if (job) void run(() => status(job));
  }, []);
  const [artifact, setArtifact] = useState<Artifact | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const locked = useRef(false);
  const lifetime = useRef(new AbortController());
  useEffect(() => () => lifetime.current.abort(), []);
  const prefix = `/api/v1/workspaces/${workspace}`;
  const allowed = roles.some((role) =>
    ["reviewer", "approver", "auditor"].includes(role),
  );
  async function request(path: string, init: RequestInit = {}) {
    const response = await fetch(path, {
      ...init,
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
      signal: lifetime.current.signal,
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => null);
      // Rejections do not authorize discarding a previously ambiguous command.
      // Preserve its key/body/revision even when an operator must reconcile it.
      if (init.method === "POST") {
        if (response.status === 409 && detail?.detail === "IDEMPOTENCY_DIGEST_MISMATCH")
          throw new Error("Export request identity conflicts with an earlier command. Do not create a new key. Ask an operator to reconcile the original command.");
        if (response.status === 503)
          throw new Error("Submission outcome unknown. Retry the same export request; the original key and case revision are retained. Do not create another job.");
        if (response.status === 422 && ["EXPORT_INPUT_TOO_LARGE", "EXPORT_TOO_LARGE"].includes(detail?.detail))
          throw new Error("Export exceeds metadata size limits. Ask an operator to reconcile this case and the original command; retrying unchanged parameters cannot reduce its size. Original request identity retained.");
      }
      if (response.status === 409 && detail?.detail === "RETENTION_DELETED")
        throw new Error(
          "Decision evidence was deleted. This case cannot be exported.",
        );
      const messages: Record<number, string> = {
        412: "Case changed. Original request identity retained. Refresh the case for inspection; ask an operator to reconcile the original command before any new export.",
        422: "Export parameters were rejected. Only metadata-only audit-v1 is supported.",
        401: "Session ended. Sign in again; no artifact was downloaded.",
        403: "Export access denied. Current permissions do not allow this request.",
        404: "Export unavailable for this account and workspace.",
        410: "Export expired or deleted. Previously issued URLs no longer grant access.",
        409: "Artifact is not ready. Refresh export status.",
        503: "Export service unavailable. Retry status; do not create another job.",
      };
      throw new Error(
        messages[response.status] ||
          `Export request failed (${response.status}).`,
      );
    }
    return response;
  }
  async function run(action: () => Promise<void>) {
    if (locked.current) return;
    locked.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await action();
    } catch (e) {
      if (lifetime.current.signal.aborted) return;
      setArtifact(null);
      setError((e as Error).message);
    } finally {
      locked.current = false;
      if (!lifetime.current.signal.aborted) setBusy(false);
    }
  }
  async function status(id: string) {
    const data = await (await request(`${prefix}/artifacts/${id}`)).json();
    if (lifetime.current.signal.aborted) return;
    if (data.artifact_job_id !== id)
      throw new Error("Export identity did not match.");
    setArtifact(data);
  }
  async function enqueue() {
    if (recovery.blocked)
      throw new Error("Export recovery is unresolved. Retry the recovery read first.");
    const command = pending || { key: crypto.randomUUID(), version, job: "" };
    remember(command);
    let result;
    try {
      const response = await request(`${prefix}/cases/${caseId}/exports`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrf,
          "Idempotency-Key": command.key,
          "If-Match": `"case-v${command.version}"`,
        },
        body: JSON.stringify({
          case_version: command.version,
          include_originals: false,
          redaction_profile: "audit-v1",
        }),
      });
      result = await response.json();
      if (lifetime.current.signal.aborted) return;
    } catch (e) {
      if (e instanceof TypeError)
        throw new Error(
          "Submission outcome unknown. Retry the same export request; no new key will be created.",
        );
      throw e;
    }
    if (typeof result?.artifact_job_id !== "string" || !uuid.test(result.artifact_job_id))
      throw new Error(
        "Invalid export job identity. Retry the same export request.",
      );
    remember({ ...command, job: result.artifact_job_id });
    await status(result.artifact_job_id);
  }
  async function download() {
    const response = await request(`${prefix}/artifacts/${job}?download=true`);
    const limit = 262144;
    const reader = response.body?.getReader();
    if (!reader)
      throw new Error("Export integrity check failed. Nothing downloaded.");
    const buffer = new Uint8Array(limit);
    let size = 0;
    try {
      const declared = response.headers.get("Content-Length");
      if (declared !== null && /^\d+$/.test(declared) && Number(declared) > limit) {
        await reader.cancel().catch(() => {});
        throw new Error("Export integrity check failed. Nothing downloaded.");
      }
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        if (lifetime.current.signal.aborted || value.byteLength > limit - size) {
          await reader.cancel().catch(() => {});
          throw new Error("Export integrity check failed. Nothing downloaded.");
        }
        buffer.set(value, size);
        size += value.byteLength;
      }
    } finally {
      reader.releaseLock();
    }
    // Only our bounded copy is accumulated. Browser/network chunk allocation is
    // outside this guard; an oversized delivered chunk is never copied or hashed.
    const bytes = buffer.subarray(0, size);
    const hash = Array.from(
      new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
      (n) => n.toString(16).padStart(2, "0"),
    ).join("");
    if (
      bytes.byteLength > 262144 ||
      bytes.byteLength !== artifact?.bytes ||
      hash !== artifact.sha256 ||
      response.headers.get("ETag") !== `"sha256-${hash}"`
    )
      throw new Error("Export integrity check failed. Nothing downloaded.");
    if (lifetime.current.signal.aborted) return;
    const url = URL.createObjectURL(
      new Blob([bytes], { type: "application/json" }),
    );
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `folio-evidence-${job}.json`;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    setNotice(
      "Evidence JSON downloaded. Exact artifact digest checked; originals excluded.",
    );
  }
  return (
    <section aria-label="Permissioned evidence export">
      <p className="small muted">
        Metadata-only audit-v1 · originals excluded. This export does not
        perform new verification or establish release readiness. Each status and
        download request checks current server authorization.
      </p>
      {!allowed && (
        <p>
          Reviewer, approver or auditor permission is required to create an
          export.
        </p>
      )}
      <button
        className="primary"
        disabled={!allowed || busy || !!job || recovery.blocked}
        onClick={() => run(enqueue)}
      >
        {pending && !job ? "Retry same export request" : "Export evidence JSON"}
      </button>
      {recovery.blocked && (
        <>
          <p role="alert">
            Export recovery identity is unreadable or invalid. Creation is blocked:
            an earlier request may have committed. Retry the recovery read. Do not
            clear storage or create a new key; if it remains invalid, an operator
            must reconcile the original command before an explicit disposition.
          </p>
          <button disabled={busy} onClick={() => setRecovery(restore())}>
            Retry recovery read
          </button>
        </>
      )}
      {job && (
        <>
          <p className="mono">Job: {job}</p>
          <p role="status" data-testid="export-state">
            {artifact ? `Export ${artifact.state}` : "Export status unknown"}
          </p>
          <button disabled={busy} onClick={() => run(() => status(job))}>
            Refresh export status
          </button>
          <button
            disabled={busy || artifact?.state !== "ready"}
            onClick={() => run(download)}
          >
            Download evidence JSON
          </button>
          <p className="small">
            Artifact SHA-256:{" "}
            <span className="mono">{artifact?.sha256 || "Not published"}</span>
          </p>
          <pre tabIndex={0} aria-label="Export manifest">
            {JSON.stringify(artifact?.manifest, null, 2)}
          </pre>
        </>
      )}
      {busy && <p role="status">Waiting for export server response…</p>}
      {error && <p role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
    </section>
  );
}
