import React, { useEffect, useState, useRef } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";
import { EvidenceExport } from "./EvidenceExport";
import {
  downloadProblem,
  originalControl,
  storedOriginalCount,
} from "./originalAvailability";
import {
  amountWithCurrency,
  displayCurrency,
  displayUnit,
  minorUnitQuantum,
  quantityWithUnit,
} from "./dimensions";
import {
  createRequestTransport,
  type PendingMutationSummary,
  type RequestPrincipal,
} from "./requestTransport";
import { workspaceLabel } from "./workspaceLabel";
type Session = {
  username: string;
  roles: string[];
  csrf: string;
  workspace: string;
};
type Word = { quote: string; start: number; end: number; boxes: number[][] };
type Source = {
  id: string;
  name: string;
  sha256: string;
  original_deleted?: boolean;
  pages: { number: number; text: string; words: Word[]; text_sha256: string }[];
};
type ReceiptRow = {
  key: string;
  receipt_id: string;
  line_id: string;
  quantity: string;
  currency?: string;
  unit?: string;
  allocated?: string;
  evidence: {
    source_id: string;
    source_sha256: string;
    source_version: number;
    page: number;
    start: number;
    end: number;
    quote: string;
  }[];
};
type Proposal = {
  id: string;
  version: number;
  state: string;
  patch: Record<string, string>;
  digest: string;
  target_digest: string;
  target_revision: number;
  operation_key: string;
  prepared_by: string;
  reason: string;
  before_snapshot?: Record<string, string>;
  allocation_manifest?: { rows: ReceiptRow[]; total: string; policy: string };
  retained_balance?: string;
  gate?: string;
  approved_by?: string;
  expires: string;
};
type Verification = {
  outcome: string;
  checks: Record<string, boolean>;
  current_drift?: boolean;
  observation?: {
    receipt: {
      before_snapshot: Record<string, string>;
      after_snapshot: Record<string, string>;
    };
    current_record: Record<string, string>;
  };
};
type HumanCorrection = {
  field: string;
  previous_value?: unknown;
  value?: unknown;
  reason?: string;
  actor?: string;
  kind?: string;
  at?: string;
};
function latestHumanCorrection(
  c: { corrections?: unknown[] },
  field: string,
): HumanCorrection | undefined {
  return (c.corrections || [])
    .map((item) => item as HumanCorrection)
    .filter((item) => item?.kind === "human" && item.field === field)
    .at(-1);
}
function HumanBadge({ c, field }: { c: { corrections?: unknown[] }; field: string }) {
  const latest = latestHumanCorrection(c, field);
  if (!latest) return null;
  return (
    <span
      className="badge warn human-correction"
      data-testid={`human-corrected-${field}`}
      title={`Corrected by ${latest.actor ?? "a reviewer"}: ${latest.reason ?? ""}`}
    >
      Human-corrected
    </span>
  );
}
type Case = {
  id: string;
  label: string;
  invoice: string;
  supplier: string;
  state: string;
  version: number;
  sources: Source[];
  source_digest: string;
  invoice_quantity: string;
  ordered_quantity: string;
  received_quantity: string;
  unit_price: string;
  currency?: string;
  unit?: string;
  tax_rate: string;
  invoice_face_amount: string;
  proposal: Proposal | null;
  verification: Verification | null;
  jobs?: {
    id: string;
    state: string;
    fence: number;
    attempt: string;
    result: unknown;
  }[];
  retention?: {
    legal_hold: boolean;
    original_days: number;
    decision_evidence_days: number;
    originals_deleted_at?: string;
  };
  intake_digest?: string;
  // Validated by originalAvailability.ts; an older API omits it entirely.
  original_availability?: unknown;
  missing_fields?: string[];
  conflicting_fields?: string[];
  receipt_manifest?: { rows: ReceiptRow[]; total: string; errors: string[] };
  information_requests?: {
    id: string;
    reason: string;
    fields: string[];
    state: string;
  }[];
  target_candidates?: {
    id: string;
    revision: number;
    supplier: string;
    invoice_key: string;
  }[];
  field_provenance?: Record<string, unknown[]>;
  analysis_runs?: {
    id: string;
    status: string;
    case_version: number;
    result?: {
      error?: string;
      recommendation?: { mode: string; quantity: string; reason: string };
    };
  }[];
  sample_digest?: string;
  corrections?: unknown[];
};
type Event = {
  sequence: string;
  case_id: string;
  kind: string;
  actor: string;
  at: string;
  body: unknown;
};
type AccessRequest = {
  id: string;
  requested_by: string;
  target_username: string;
  action: string;
  role: string | null;
  reason: string;
  status: string;
  version: number;
  created_at: string;
  reviewed_by: string | null;
  reviewed_at: string | null;
  review_reason: string | null;
};
type WorkspaceSettings = {
  settings_version: number;
  policy_version: number;
  auth_epoch: number;
  members: { username: string; roles: string[]; active: boolean }[];
  requests: AccessRequest[];
};
const screens = [
  ["overview", "Workspace overview"],
  ["inbox", "Document inbox"],
  ["import", "Import documents"],
  ["document", "Document & extracted fields"],
  ["reconciliation", "Reconciliation workspace"],
  ["decision", "Resolve an exception"],
  ["approvals", "Approval queue"],
  ["changes", "Review proposed changes"],
  ["execution", "Execution detail"],
  ["recovery", "Recover an interrupted run"],
  ["records", "Verified business records"],
  ["evidence", "Evidence package"],
  ["activity", "Activity & audit history"],
  ["settings", "Workspace & access settings"],
];

const MAX_INTAKE_FILES = 10;
const MAX_INTAKE_FILE_BYTES = 20 * 1024 * 1024;

function validateSelectedFiles(files: File[]) {
  if (files.length > MAX_INTAKE_FILES)
    throw new Error(`Select at most ${MAX_INTAKE_FILES} files. No files were read or uploaded.`);
  for (const file of files) {
    const extension = file.name.split(".").pop()?.toLowerCase();
    if (!extension || !["pdf", "csv", "xlsx"].includes(extension))
      throw new Error(`${file.name} is not a supported PDF, CSV, or XLSX file. No files were read or uploaded.`);
    if (file.size <= 0)
      throw new Error(`${file.name} is empty. No files were read or uploaded.`);
    if (file.size > MAX_INTAKE_FILE_BYTES)
      throw new Error(`${file.name} exceeds the 20 MiB per-file limit. No files were read or uploaded.`);
  }
}

function App() {
  const [session, setSession] = useState<Session | null>(null),
    [pendingMutation, setPendingMutation] =
      useState<PendingMutationSummary | null>(null),
    [needsMutationReconciliation, setNeedsMutationReconciliation] =
      useState(false),
    [checking, setChecking] = useState(true),
    [route, setRoute] = useState(location.hash.slice(1) || "overview"),
    [items, setItems] = useState<Case[]>([]),
    [overviewLastSuccess, setOverviewLastSuccess] = useState<number | null>(null),
    [overviewUnavailable, setOverviewUnavailable] = useState(false),
    [active, setActive] = useState<Case | null>(null),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [busy, setBusy] = useState(false),
    [search, setSearch] = useState(""),
    [username, setUsername] = useState(""),
    [password, setPassword] = useState(""),
    [reason, setReason] = useState(""),
    [qty, setQty] = useState("100"),
    [mode, setMode] = useState("partial"),
    [invoice, setInvoice] = useState(""),
    [correction, setCorrection] = useState(""),
    [sourceIndex, setSourceIndex] = useState(0),
    [sourcePageNumber, setSourcePageNumber] = useState(1),
    [word, setWord] = useState<Word | null>(null),
    [events, setEvents] = useState<Event[]>([]),
    [accessSettings, setAccessSettings] = useState<WorkspaceSettings | null>(null),
    [accessTarget, setAccessTarget] = useState(""),
    [accessAction, setAccessAction] = useState("grant_role"),
    [accessRole, setAccessRole] = useState("reviewer"),
    [accessReason, setAccessReason] = useState(""),
    [accessReviewReason, setAccessReviewReason] = useState(""),
    [actorFilter, setActorFilter] = useState("all");
  const [localFiles, setLocalFiles] = useState<{ file: File; role: string }[]>(
    [],
  );
  const [rights, setRights] = useState(false);
  const [attachSupport, setAttachSupport] = useState(false);
  const [infoReason, setInfoReason] = useState("");
  const [editField, setEditField] = useState("received_quantity");
  const [editValue, setEditValue] = useState("");
  const [selectedTarget, setSelectedTarget] = useState("");
  const [delegateProposal, setDelegateProposal] = useState(false);
  const [streamStatus, setStreamStatus] = useState("connecting");
  const dialog = useRef<HTMLDialogElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const draftCase = useRef<string | null>(null);
  const caseSelectionSequence = useRef(0);
  const requestTransportRef = useRef<ReturnType<
    typeof createRequestTransport
  > | null>(null);
  if (!requestTransportRef.current)
    requestTransportRef.current = createRequestTransport();
  const requestTransport = requestTransportRef.current;
  const durabilityProblem = requestTransport.durabilityProblem();
  const prefix = "/api/v1/workspaces/" + session?.workspace;
  const role = (r: string) => session?.roles.includes(r) || false;
  const principal: RequestPrincipal | undefined = session
    ? {
        actor: session.username,
        workspace: session.workspace,
        csrf: session.csrf,
      }
    : undefined;
  // Unsent decision drafts survive a page refresh in this tab only (sessionStorage).
  const draftKey = (caseId: string) =>
    session ? `folio-draft:${session.workspace}:${session.username}:${caseId}` : "";
  function restoreDraft(caseId: string) {
    try {
      const raw = sessionStorage.getItem(draftKey(caseId));
      if (!raw) return;
      const d = JSON.parse(raw);
      if (typeof d.qty === "string") setQty(d.qty);
      if (typeof d.reason === "string") setReason(d.reason);
      if (typeof d.mode === "string") setMode(d.mode);
      if (typeof d.invoice === "string") setInvoice(d.invoice);
      if (typeof d.correction === "string") setCorrection(d.correction);
    } catch {
      /* storage unavailable or corrupt: keep server values */
    }
  }
  function clearDraft(caseId: string) {
    try {
      sessionStorage.removeItem(draftKey(caseId));
    } catch {
      /* storage unavailable */
    }
  }
  useEffect(() => {
    const caseId = draftCase.current;
    if (!session || !caseId || active?.id !== caseId) return;
    try {
      sessionStorage.setItem(
        draftKey(caseId),
        JSON.stringify({ qty, reason, mode, invoice, correction }),
      );
    } catch {
      /* storage unavailable: draft lives in memory only */
    }
  }, [qty, reason, mode, invoice, correction, active?.id, session]);
  function syncPendingMutation() {
    setPendingMutation(requestTransport.pendingSummary());
    setNeedsMutationReconciliation(requestTransport.needsReconciliation());
  }
  async function api(
    path: string,
    method = "GET",
    body?: unknown,
    version?: string,
  ) {
    try {
      const data = await requestTransport.request(
        path,
        method,
        body,
        version,
        principal,
      );
      syncPendingMutation();
      return data;
    } catch (error) {
      syncPendingMutation();
      throw error;
    }
  }
  function go(id: string) {
    location.hash = id;
    setRoute(id);
    setError("");
    setNotice("");
    setWord(null);
  }
  async function select(c: Case, id: string) {
    const sequence = ++caseSelectionSequence.current;
    localStorage.setItem("folio-selected-case", c.id);
    setSelectedTarget("");
    const detail = await api(prefix + "/cases/" + c.id);
    if (
      sequence !== caseSelectionSequence.current ||
      localStorage.getItem("folio-selected-case") !== c.id
    )
      return;
    setActive(detail);
    setInvoice(c.invoice);
    restoreDraft(c.id);
    draftCase.current = c.id;
    go(id);
  }
  async function refresh() {
    if (!session) return;
    const sequence = caseSelectionSequence.current;
    let data: { items: Case[] };
    try {
      data = await api(prefix + "/cases");
    } catch (error) {
      if (sequence === caseSelectionSequence.current) setOverviewUnavailable(true);
      throw error;
    }
    if (sequence !== caseSelectionSequence.current) return;
    setOverviewLastSuccess(Date.now());
    setOverviewUnavailable(false);
    setItems(data.items);
    const id = localStorage.getItem("folio-selected-case");
    const selected =
      data.items.find((c: Case) => c.id === id) ||
      data.items.find((c: Case) => c.sample_digest);
    if (selected) {
      const d = await api(prefix + "/cases/" + selected.id);
      if (
        sequence !== caseSelectionSequence.current ||
        localStorage.getItem("folio-selected-case") !== selected.id
      )
        return;
      setActive(d);
      if (draftCase.current !== selected.id) {
        setSelectedTarget("");
        setInvoice(d.invoice);
        setQty(d.received_quantity || "");
        setReason("");
        restoreDraft(selected.id);
        draftCase.current = selected.id;
      }
    }
  }
  async function act(work: () => Promise<void>) {
    setError("");
    setBusy(true);
    try {
      await work();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function retryUncertainMutation() {
    if (!principal) return;
    const retryingLogout = pendingMutation?.logout === true;
    setBusy(true);
    setError("");
    try {
      await requestTransport.retryPending(principal);
      syncPendingMutation();
      dialog.current?.close();
      if (retryingLogout) {
        setSession(null);
        draftCase.current = null;
        setInvoice("");
        setReason("");
        setCorrection("");
        setPassword("");
        setUsername("");
        setActive(null);
        setItems([]);
        setEvents([]);
        setError("");
        setNotice("");
        return;
      }
      setNotice(
        "The original request received a confirmed response. Refreshing the current workspace.",
      );
      try {
        await refresh();
        if (route === "activity") {
          const data = await api(prefix + "/activity?actor_type=" + actorFilter);
          setEvents(data.events);
        }
        setNotice("The original request is confirmed and the workspace is refreshed.");
      } catch {
        setError(
          "The original request is confirmed, but the workspace refresh failed. Retry the refresh to load current state.",
        );
      }
    } catch (e) {
      syncPendingMutation();
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function lookupReloadedMutation() {
    if (!principal) return;
    setBusy(true);
    setError("");
    try {
      const retryingLogout = pendingMutation?.logout === true;
      const outcome = await requestTransport.lookupDurableOutcome(principal);
      syncPendingMutation();
      if (outcome === "committed") {
        if (retryingLogout) {
          await api("/api/v1/auth/logout", "POST", {});
          setSession(null);
          setActive(null);
          setItems([]);
          return;
        }
        setNotice("The server confirms this request was recorded. Refreshing current workspace state.");
        await refresh();
        if (route === "settings") setAccessSettings(await api(prefix + "/settings"));
      } else if (outcome === "not_recorded") {
        setNotice("No command receipt is recorded. The request remains blocked; inspect authorized case and activity history before acknowledging reconciliation.");
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  function acknowledgeReconciliation() {
    try {
      if (
        principal &&
        window.confirm(
          "Confirm that you reviewed current workspace records and activity and accept that allowing new changes may follow an earlier change whose response was lost.",
        ) &&
        requestTransport.clearAfterExplicitReconciliation(principal)
      ) {
        syncPendingMutation();
        setError("");
        setNotice("Manual reconciliation acknowledged. New changes are enabled.");
      }
    } catch (e) {
      setError((e as Error).message);
    }
  }
  function abandonPendingForAnotherSignIn() {
    if (
      !requestTransport.hasUnresolvedMutation() ||
      !window.confirm(
        "An earlier action may already have committed. This only erases the saved retry from this browser; it does not cancel or undo anything on the server. Confirm that you reviewed the case and audit history you are authorized to access and accept the risk before proceeding.",
      )
    )
      return;
    try {
      if (requestTransport.abandonPendingLocally()) {
        syncPendingMutation();
        setError("");
      }
    } catch (e) { setError((e as Error).message); }
  }
  useEffect(() => {
    syncPendingMutation();
  }, [session?.username, session?.workspace]);
  useEffect(() => {
    setSourceIndex(0);
    setSourcePageNumber(active?.sources[0]?.pages[0]?.number || 1);
    setWord(null);
  }, [active?.id]);
  useEffect(() => {
    if (!pendingMutation) return;
    const warnBeforeLeave = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warnBeforeLeave);
    return () => window.removeEventListener("beforeunload", warnBeforeLeave);
  }, [pendingMutation]);
  useEffect(() => {
    fetch("/api/v1/auth/session")
      .then(async (r) => {
        if (!r.ok) return;
        const restored = await r.json();
        if (!requestTransport.principalMatchesPending(restored)) {
          setError(
            "An earlier request is unresolved. Sign in as the account that started it before making more changes.",
          );
          return;
        }
        setSession(restored);
      })
      .catch(() => setError("API unavailable. Start the local API and retry."))
      .finally(() => setChecking(false));
    const h = () => setRoute(location.hash.slice(1) || "overview");
    window.addEventListener("hashchange", h);
    return () => window.removeEventListener("hashchange", h);
  }, []);
  useEffect(() => {
    if (!session) return;
    refresh().catch((e) => setError(e.message));
    const timer = setInterval(
      () =>
        refresh().catch(() =>
          setError(
            "Connection lost. Last confirmed state retained; protected actions require a fresh response.",
          ),
        ),
      1500,
    );
    const source = new EventSource(prefix + "/events");
    let pending: ReturnType<typeof setTimeout> | undefined;
    source.onopen = () => setStreamStatus("connected");
    source.onerror = () => setStreamStatus("reconnecting — polling fallback");
    source.onmessage = () => {
      if (pending) clearTimeout(pending);
      pending = setTimeout(
        () => refresh().catch(() => setStreamStatus("refresh unavailable")),
        100,
      );
    };
    source.addEventListener("authorization-lost", () => {
      source.close();
      setError("Session authorization ended. Sign in again.");
      setSession(null);
    });
    source.addEventListener("maintenance", () => {
      source.close();
      setStreamStatus("maintenance window");
      setNotice("Folio is quiescing for maintenance. New API requests are temporarily paused.");
    });
    return () => {
      clearInterval(timer);
      if (pending) clearTimeout(pending);
      source.close();
    };
  }, [session]);
  useEffect(() => {
    if (!session || !pendingMutation?.durableOnly) return;
    void lookupReloadedMutation();
  }, [session?.username, session?.workspace, pendingMutation?.durableOnly]);
  useEffect(() => {
    heading.current?.focus();
    if (session && route === "settings")
      api(prefix + "/settings")
        .then((d) => setAccessSettings(d))
        .catch((e) => setError(e.message));
  }, [route, session]);
  useEffect(() => {
    if (!session || route !== "activity") return;
    api(prefix + "/activity?actor_type=" + actorFilter)
      .then((d) => setEvents(d.events))
      .catch((e) => setError(e.message));
  }, [session, route, actorFilter]);
  const c = active,
    p = c?.proposal,
    v = c?.verification;
  const analysis = c?.analysis_runs?.at(-1);
  const title =
    screens.find((s) => s[0] === route)?.[1] || "Workspace overview";
  const currentWorkspace = workspaceLabel(session?.workspace);
  const Badge = ({ state }: { state: string }) => (
    <span
      className={
        "badge " +
        (state === "verified" ? "ok" : state === "uncertain" ? "error" : "warn")
      }
    >
      {state.replaceAll("_", " ")}
    </span>
  );
  function panel(title: string, children: React.ReactNode) {
    return (
      <section className="panel">
        <div className="panelhead">
          <h2>{title}</h2>
        </div>
        <div className="body">{children}</div>
      </section>
    );
  }
  const caseTable = (list: Case[]) => (
    <div className="tablewrap" tabIndex={0} role="region" aria-label="Cases">
      <table>
        <thead>
          <tr>
            <th>Packet / supplier</th>
            <th>Invoice face</th>
            <th>Status</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody>
          {list.map((x) => (
            <tr key={x.id}>
              <td>
                <strong>{x.invoice}</strong>
                <small>
                  {x.supplier} · {x.label}
                </small>
              </td>
              <td>{amountWithCurrency(x.invoice_face_amount, x.currency)}</td>
              <td>
                <Badge state={x.state} />
              </td>
              <td>
                <button
                  className="link"
                  onClick={() => act(() => select(x, "document"))}
                >
                  Open packet
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {!list.length && (
        <div className="empty">
          <strong>No packets match</strong>
          <p>Import an authored sample packet or clear your search.</p>
          <button onClick={() => setSearch("")}>Clear search</button>
        </div>
      )}
    </div>
  );
  const requireCase = (
    <div className="empty">
      <strong>No packet selected</strong>
      <p>Import the authored packet to inspect real source artifacts.</p>
      <button onClick={() => go("import")}>Import documents</button>
    </div>
  );
  async function downloadOriginal(source: Source) {
    if (!c) return;
    const response = await fetch(
      prefix +
        `/cases/${encodeURIComponent(c.id)}/sources/${encodeURIComponent(source.id)}/original`,
      { credentials: "same-origin" },
    );
    if (!response.ok) {
      const problem = await response.json().catch(() => ({}));
      // Availability is advisory; a download-time denial refreshes the reported state.
      await refresh().catch(() => undefined);
      throw Error(downloadProblem(problem.detail));
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download =
      source.name
        .split(/[\\/]/)
        .pop()
        ?.replace(/[\u0000-\u001f\u007f]/g, "") || "original";
    document.body.appendChild(link);
    try {
      link.click();
      setNotice(
        "Original download requested. The server checked its stored byte digest; no document was changed.",
      );
    } finally {
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
  }
  async function saveDecision() {
    if (!c) return;
    await api(
      prefix + `/cases/${c.id}/decisions`,
      "POST",
      { mode, quantity: qty, reason, evidence_digest: c.source_digest },
      `"case-v${c.version}"`,
    );
    clearDraft(c.id);
    await refresh();
    go(mode === "partial" ? "changes" : "inbox");
  }
  async function approve() {
    if (!p) return;
    await api(
      prefix + `/proposals/${p.id}/approvals`,
      "POST",
      { intent_digest: p.digest, confirmation: "approve" },
      `"proposal-v${p.version}"`,
    );
    dialog.current?.close();
    await refresh();
    go("execution");
  }
  async function signout() {
    await api("/api/v1/auth/logout", "POST", {});
    if (draftCase.current) clearDraft(draftCase.current);
    setSession(null);
    draftCase.current = null;
    setInvoice("");
    setReason("");
    setCorrection("");
    setPassword("");
    setUsername("");
    setActive(null);
    setItems([]);
    setError("");
  }
  async function submitAccessRequest() {
    if (!accessSettings || !accessTarget) return;
    await api(
      prefix + "/settings/access-requests",
      "POST",
      {
        target_username: accessTarget,
        action: accessAction,
        role: ["grant_role", "revoke_role"].includes(accessAction) ? accessRole : null,
        reason: accessReason,
      },
      `"access-v${accessSettings.settings_version}"`,
    );
    setAccessReason("");
    setAccessSettings(await api(prefix + "/settings"));
    setNotice("Access request recorded for independent review.");
  }
  async function decideAccessRequest(request: AccessRequest, decision: "approve" | "deny") {
    await api(
      prefix + `/settings/access-requests/${request.id}/review`,
      "POST",
      { decision, reason: accessReviewReason },
      `"access-request-v${request.version}"`,
    );
    setAccessReviewReason("");
    setAccessSettings(await api(prefix + "/settings"));
  }
  let content: React.ReactNode;
  if (checking)
    return (
      <div className="login" role="status">
        Connecting to Folio…
      </div>
    );
  if (!session)
    return (
      <main className="login panel">
        <div className="eyebrow">Folio</div>
        <h1>Sign in to Folio</h1>
        <p className="muted">
          Document review with bounded, independently checked effects.
        </p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            act(async () => {
              if (!requestTransport.canAttemptLoginAs(username))
                throw Error(
                  "An earlier request is unresolved. Sign in with the same account to retry or reconcile it.",
                );
              const res = await fetch("/api/v1/auth/login", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ username, password }),
              });
              const d = await res.json();
              if (!res.ok) throw Error(d.detail);
              if (!requestTransport.principalMatchesPending(d)) {
                await fetch("/api/v1/auth/logout", {
                  method: "POST",
                  headers: { "X-CSRF-Token": d.csrf },
                });
                throw Error(
                  "This sign-in belongs to a different workspace. Sign in with the account that started the unresolved request.",
                );
              }
              setSession(d);
              setPassword("");
              go("overview");
            });
          }}
        >
          <div className="field">
            <label htmlFor="username">Username</label>
            <input
              id="username"
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
            />
          </div>
          <div className="field">
            <label htmlFor="password">Password</label>
            <input
              id="password"
              autoComplete="current-password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </div>
          <button className="primary" disabled={busy}>
            Sign in
          </button>
        </form>
        {error && (
          <p role="alert" className="ribbon error inline-alert">
            {error}
          </p>
        )}
        {durabilityProblem && (
          <p role="alert" className="ribbon error inline-alert">{durabilityProblem}</p>
        )}
        {pendingMutation && (
          <p role="alert" className="ribbon error inline-alert">
            {pendingMutation.durableOnly
              ? "A request outcome remains unresolved after reload. Its opaque key is retained, but the body is not. Sign in with the same account and workspace to check the server receipt or acknowledge a review of authorized case and audit history."
              : "A request from this browser has an unknown outcome. Sign in with the same account and workspace to retry that exact request. Its body stays only in memory. You may explicitly discard the local retry after reviewing authorized case and audit history."}
          </p>
        )}
        {needsMutationReconciliation && (
          <p role="alert" className="ribbon error inline-alert">
            An older request needs workspace reconciliation. Sign in with the
            account that started it before allowing new changes.
          </p>
        )}
        {(pendingMutation || needsMutationReconciliation) && (
          <button
            type="button"
            disabled={busy}
            onClick={abandonPendingForAnotherSignIn}
          >
            Acknowledge the unresolved request and continue sign-in
          </button>
        )}
        <div className="signin-note" style={{ marginTop: 24 }}>
          Local synthetic workspace only. Use separate reviewer and approver
          accounts from the README. Local analysis has read-only tools; file
          intake is sandboxed and synthetic-only. No payment or bank changes.
        </div>
      </main>
    );
  if (route === "overview")
    content = (
      <>
        <div className="summaryline">
          <div>
            <strong>{overviewUnavailable ? "Unavailable" : overviewLastSuccess === null ? "Loading" : items.length}</strong>
            <span>Durable packets</span>
          </div>
          <div>
            <strong>
              {overviewUnavailable ? "Unavailable" : overviewLastSuccess === null ? "Loading" : items.filter((x) => x.state === "awaiting_approval").length}
            </strong>
            <span>Awaiting independent approval</span>
          </div>
          <div>
            <strong>
              {overviewUnavailable ? "Unavailable" : overviewLastSuccess === null ? "Loading" : items.filter((x) => x.state === "uncertain").length}
            </strong>
            <span>Needs authoritative recovery</span>
          </div>
        </div>
        <p className="small muted" role="status" aria-live="polite">
          {overviewLastSuccess === null
            ? overviewUnavailable
              ? "Queue counts are unavailable. Retry refresh to load a current snapshot."
              : "Loading the current queue snapshot…"
            : overviewUnavailable
              ? `Current counts are unavailable. Showing the last confirmed queue snapshot from ${new Date(overviewLastSuccess).toLocaleString()}.`
              : <>Current queue snapshot received <time dateTime={new Date(overviewLastSuccess).toISOString()}>{new Date(overviewLastSuccess).toLocaleString()}</time>.</>}
        </p>
        <div className="grid">
          <div className="stack">
            {panel(
              "Prioritized worklist",
              <>
                {items
                  .filter(
                    (x) =>
                      x.sample_digest ||
                      ["awaiting_approval", "uncertain"].includes(x.state),
                  )
                  .slice(0, 5)
                  .map((x, i) => (
                    <div className="task" key={x.id}>
                      <span className="index">{i + 1}</span>
                      <div>
                        <strong>
                          {x.invoice} · {x.supplier}
                        </strong>
                        <p>
                          {x.label} · {x.state.replaceAll("_", " ")}
                        </p>
                      </div>
                      <div className="actions">
                        <button
                          onClick={() =>
                            act(() =>
                              select(
                                x,
                                x.state === "uncertain"
                                  ? "recovery"
                                  : "document",
                              ),
                            )
                          }
                        >
                          Review packet
                        </button>
                      </div>
                    </div>
                  ))}
                {!items.length && overviewUnavailable && (
                  <div className="empty">Queue data is unavailable; no zero-count snapshot is shown. Retry refresh to check current work.</div>
                )}
                {!items.length && !overviewUnavailable && overviewLastSuccess !== null && (
                  <div className="empty">
                    Start with your first authored invoice packet.
                  </div>
                )}
              </>,
            )}
            {panel("Document queue", caseTable(items.slice(0, 5)))}
          </div>
          <div className="stack">
            {panel(
              "Workspace boundaries",
              <>
                <p>
                  Invoice, purchase-order and receipt operations use the
                  packet&apos;s explicit currency. Cross-currency conversion is
                  not performed.
                </p>
                <p>No payments, supplier creation or bank edits.</p>
                <Badge state="manual review available" />
                <hr className="divider" />
                <h3>Model quality gate open</h3>
                <p className="muted small">
                  Real bounded local-weight/tool execution has been exercised on
                  an authored packet. The full held-out quality gate remains
                  open; manual review and independent approval remain mandatory.
                </p>
              </>,
            )}
            {panel(
              "Next step",
              <button className="primary" onClick={() => go("import")}>
                Import documents
              </button>,
            )}
          </div>
        </div>
      </>
    );
  else if (route === "inbox")
    content = panel(
      "Invoice packets",
      <>
        <div className="field">
          <label htmlFor="search">Search invoice, supplier or case</label>
          <input
            id="search"
            data-testid="inbox-search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search packets…"
          />
        </div>
        {caseTable(
          items.filter((x) =>
            (x.invoice + " " + x.supplier + " " + x.label)
              .toLowerCase()
              .includes(search.toLowerCase()),
          ),
        )}
      </>,
    );
  else if (route === "import")
    content = (
      <div className="grid">
        {panel(
          "Create a review packet",
          <>
            <div className="dropzone">
              <div className="docicon" style={{ margin: "0 auto 16px" }}>
                PDF
              </div>
              <h2>Import an authored packet</h2>
              <p>
                Three real digital PDFs: invoice, purchase order and goods
                receipt.
              </p>
              <button
                className="primary"
                disabled={!role("reviewer") || busy}
                onClick={() =>
                  act(async () => {
                    const d = await api(prefix + "/intakes/sample", "POST", {});
                      const sequence = ++caseSelectionSequence.current;
                      setSelectedTarget("");
                    localStorage.setItem("folio-selected-case", d.case_id);
                    const next = await api(prefix + "/cases/" + d.case_id);
                      if (
                        sequence !== caseSelectionSequence.current ||
                        localStorage.getItem("folio-selected-case") !== d.case_id
                      )
                        return;
                    setActive(next);
                    setInvoice(next.invoice);
                    await refresh();
                    go("document");
                  })
                }
              >
                Import authored sample packet
              </button>
            </div>
            <hr className="divider" />
            <h3>Import your authored synthetic documents</h3>
            <div className="field">
              <label htmlFor="local-files">Local synthetic files</label>
              <input
                id="local-files"
                type="file"
                accept=".pdf,.csv,.xlsx"
                multiple
                onChange={(e) =>
                  setLocalFiles(
                    Array.from(e.target.files || []).map((file) => ({
                      file,
                      role: "invoice",
                    })),
                  )
                }
              />
            </div>
            {localFiles.map((entry, index) => (
              <div className="field" key={index}>
                <label htmlFor={`file-role-${index}`}>
                  Document role for {entry.file.name}
                </label>
                <select
                  id={`file-role-${index}`}
                  value={entry.role}
                  onChange={(e) =>
                    setLocalFiles((files) =>
                      files.map((x, i) =>
                        i === index ? { ...x, role: e.target.value } : x,
                      ),
                    )
                  }
                >
                  <option value="invoice">Invoice</option>
                  <option value="purchase_order">Purchase order</option>
                  <option value="receipt">Receipt</option>
                </select>
              </div>
            ))}
            {c && (
              <label className="choice">
                <input
                  type="checkbox"
                  checked={attachSupport}
                  onChange={(e) => setAttachSupport(e.target.checked)}
                />
                <span>
                  Attach to selected case {c.label} (version {c.version})
                </span>
              </label>
            )}
            <label className="choice">
              <input
                type="checkbox"
                checked={rights}
                onChange={(e) => setRights(e.target.checked)}
              />
              <span>I authored these synthetic documents</span>
            </label>
            <button
              className="primary"
              disabled={
                busy || !role("reviewer") || !rights || !localFiles.length
              }
              onClick={() =>
                act(async () => {
                  validateSelectedFiles(localFiles.map((entry) => entry.file));
                  const form = new FormData();
                  form.append("rights_attestation", "authored-synthetic");
                  if (attachSupport && c) form.append("case_id", c.id);
                  for (const entry of localFiles) {
                    form.append("files", entry.file, entry.file.name);
                    form.append("document_roles", entry.role);
                  }
                  const result = await api(
                    prefix + "/intakes/files",
                    "POST",
                    form,
                    attachSupport && c ? `"case-v${c.version}"` : undefined,
                  );
                  setAttachSupport(false);
                  const sequence = ++caseSelectionSequence.current;
                  setSelectedTarget("");
                  localStorage.setItem("folio-selected-case", result.case_id);
                  await refresh();
                  if (
                    sequence !== caseSelectionSequence.current ||
                    localStorage.getItem("folio-selected-case") !== result.case_id
                  )
                    return;
                  go("document");
                  setNotice(
                    `Accepted ${result.accepted.length}; rejected ${result.rejected.length}. ${result.rejected.map((x: { name: string; reason: string }) => x.name + ": " + x.reason).join("; ")}`,
                  );
                })
              }
            >
              Import selected files
            </button>
            <p className="small muted">
              PDF + English raster OCR, UTF-8 CSV and XLSX; 20 MiB/file, 10
              files/packet. Rootless networkless parser containers. Unknown
              labels, missing values and formulas require human review. Do not
              import private real documents.
            </p>
            <div className="file">
              <div className="docicon">PDF</div>
              <div>
                <strong>invoice.pdf · INV-1048</strong>
                <small>
                  Example packet · 120 each · USD 6,220.80 face value
                </small>
              </div>
            </div>
            <div className="file">
              <div className="docicon">PDF</div>
              <div>
                <strong>purchase-order.pdf · PO-0732</strong>
                <small>120 each ordered · Aster Supply Co.</small>
              </div>
            </div>
            <div className="file">
              <div className="docicon">PDF</div>
              <div>
                <strong>receipt.pdf · GR-208</strong>
                <small>
                  100 each confirmed · no duplicate receipt allocation
                </small>
              </div>
            </div>
          </>,
        )}
        {panel(
          "Intake availability",
          <>
            <Badge state="synthetic fixtures only" />
            <p style={{ marginTop: 12 }}>
              PDFs are independently authored CC0 test inputs. Hashes are
              checked against a local manifest and real text/character geometry
              is extracted.
            </p>
            <p className="muted small">
              User-attested synthetic files are parsed in bounded, non-root,
              networkless, read-only containers without host mounts. Format/OCR
              support has finite fixtures, not a full hostile-parser security
              review.
            </p>
            <p>Duplicate authored packets link to the existing case.</p>
          </>,
        )}
      </div>
    );
  else if (!c && !["approvals", "activity", "settings"].includes(route))
    content = requireCase;
  else if (route === "document" && c) {
    const s = c.sources[sourceIndex] || c.sources[0];
    const sourcePages = s?.pages || [];
    const selectedPage = sourcePages.find((page) => page.number === sourcePageNumber) || sourcePages[0];
    content = (
      <>
        <div className="actions" style={{ marginBottom: 20 }}>
          <button className="primary" onClick={() => go("reconciliation")}>
            Compare documents
          </button>
          {c.sources.map((s, i) => (
            <button
              key={s.id}
              className={i === sourceIndex ? "primary" : ""}
              aria-pressed={i === sourceIndex}
              onClick={() => {
                setSourceIndex(i);
                setSourcePageNumber(c.sources[i]?.pages[0]?.number || 1);
                setWord(null);
              }}
            >
              {s.name}
            </button>
          ))}
          {s &&
            (() => {
              const original = originalControl(c.original_availability, s.id);
              return (
                <>
                  <button
                    disabled={busy || !original.downloadable}
                    aria-describedby="document-original-availability"
                    data-testid="document-original-download"
                    onClick={() => act(() => downloadOriginal(s))}
                  >
                    {original.downloadable
                      ? "Download uploaded original"
                      : original.label}
                  </button>
                  <small
                    id="document-original-availability"
                    className="muted"
                    data-state={original.state}
                  >
                    {original.explanation}
                  </small>
                </>
              );
            })()}
        </div>
        <div className="grid">
          <section className="panel">
            <div className="panelhead">
              <h2>{s?.name || "No source artifacts"}</h2>
              <span className="badge">
                {selectedPage ? `Page ${selectedPage.number} of ${sourcePages.length}` : "No parsed pages"}
              </span>
            </div>
            {sourcePages.length > 0 && (
              <div className="actions" aria-label="Document page navigation">
                {sourcePages.map((page) => (
                  <button
                    key={page.number}
                    type="button"
                    className={page.number === selectedPage?.number ? "primary" : ""}
                    aria-current={page.number === selectedPage?.number ? "page" : undefined}
                    aria-label={`Show page ${page.number}`}
                    onClick={() => { setSourcePageNumber(page.number); setWord(null); }}
                  >
                    Page {page.number}
                  </button>
                ))}
              </div>
            )}
            <div className="sourcepane">
              <div className="paper">
                <div className="eyebrow">
                  Actual parser output · author-created fixture
                </div>
                <div className="source-text" aria-label={selectedPage ? `Page ${selectedPage.number} source text` : "Source text unavailable"}>
                  {selectedPage?.words.length ? selectedPage.words.map((w, i) => (
                    <React.Fragment key={i}>
                      <button
                        className={
                          "source-word " + (word === w ? "selected" : "")
                        }
                        onClick={() => setWord(w)}
                        title={`Exact parser offsets ${w.start}–${w.end}`}
                      >
                        {w.quote}
                      </button>{" "}
                    </React.Fragment>
                  )) : selectedPage?.text || (s ? "This source has no parsed page text available." : "No source artifacts are attached to this case.")}
                </div>
              </div>
            </div>
          </section>
          <div className="stack">
            {panel(
              "Extracted values & review",
              <>
                <div className="keyvalue">
                  <span>Invoiced quantity</span>
                  <div className="keyvalue-value">
                    <strong>
                      {quantityWithUnit(c.invoice_quantity, c.unit)}
                    </strong>
                    <HumanBadge c={c} field="invoice_quantity" />
                  </div>
                </div>
                <div className="keyvalue">
                  <span>Received quantity</span>
                  <div className="keyvalue-value">
                    <strong>
                      {quantityWithUnit(c.received_quantity, c.unit)}
                    </strong>
                    <HumanBadge c={c} field="received_quantity" />
                  </div>
                </div>
                <div className="keyvalue">
                  <span>Invoice face total</span>
                  <div className="keyvalue-value">
                    <strong>
                      {amountWithCurrency(c.invoice_face_amount, c.currency)}
                    </strong>
                    <HumanBadge c={c} field="invoice_face_amount" />
                  </div>
                </div>
                <hr className="divider" />
                <div className="field">
                  <label htmlFor="edit-field">Field to correct</label>
                  <select
                    id="edit-field"
                    value={editField}
                    onChange={(e) => {
                      setEditField(e.target.value);
                      setEditValue(
                        String(
                          (c as unknown as Record<string, unknown>)[
                            e.target.value
                          ] || "",
                        ),
                      );
                    }}
                  >
                    {[
                      "received_quantity",
                      "ordered_quantity",
                      "invoice_quantity",
                      "unit_price",
                      "tax_rate",
                      "invoice_face_amount",
                      "supplier",
                      "currency",
                      "unit",
                    ].map((field) => (
                      <option key={field} value={field}>
                        {field.replaceAll("_", " ")}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label htmlFor="edit-value">Corrected value</label>
                  <input
                    id="edit-value"
                    value={editValue}
                    onChange={(e) => setEditValue(e.target.value)}
                  />
                </div>
                <button
                  data-testid="save-general-field"
                  disabled={busy || !role("reviewer")}
                  onClick={() =>
                    act(async () => {
                      await api(
                        prefix + `/cases/${c.id}/fields/${editField}`,
                        "PATCH",
                        { typed_value: editValue, reason: correction },
                        `"case-v${c.version}"`,
                      );
                      await refresh();
                      setNotice(
                        "Versioned human correction saved; no source provenance fabricated.",
                      );
                    })
                  }
                >
                  Save corrected value
                </button>
                {(c.corrections || []).length > 0 && (
                  <div className="human-corrections" aria-label="Human corrections">
                    <strong>Human corrections</strong>
                    <ul>
                      {(c.corrections || []).map((item, i) => {
                        const h = item as HumanCorrection;
                        return (
                          <li key={i} data-testid="human-correction-entry">
                            <span className="badge warn">Human</span>{" "}
                            {h.field.replaceAll("_", " ")}:{" "}
                            {String(h.previous_value ?? "—")} → {String(h.value ?? "—")} ·{" "}
                            {h.actor} · {h.reason}
                          </li>
                        );
                      })}
                    </ul>
                  </div>
                )}
                <details>
                  <summary>Versioned field provenance</summary>
                  <pre aria-label="Field provenance">
                    {JSON.stringify(c.field_provenance || {}, null, 2)}
                  </pre>
                </details>
                <hr className="divider" />
                <div className="field">
                  <label htmlFor="invoice">Invoice identity</label>
                  <input
                    id="invoice"
                    data-testid="invoice-field"
                    value={invoice}
                    onChange={(e) => setInvoice(e.target.value)}
                  />
                </div>
                <div className="field">
                  <label htmlFor="correction">Correction reason</label>
                  <textarea
                    id="correction"
                    value={correction}
                    onChange={(e) => setCorrection(e.target.value)}
                  />
                </div>
                <button
                  data-testid="save-field"
                  disabled={!role("reviewer") || busy}
                  onClick={() =>
                    act(async () => {
                      await api(
                        prefix + `/cases/${c.id}/fields/invoice`,
                        "PATCH",
                        { typed_value: invoice, reason: correction },
                        `"case-v${c.version}"`,
                      );
                      await refresh();
                      setNotice(
                        "Human correction saved. Previous evidence retained; unsealed proposal superseded.",
                      );
                    })
                  }
                >
                  Save field correction
                </button>
              </>,
            )}
            {panel(
              "Local model analysis",
              <>
                <p className="small muted">
                  Real local Spark weights read source evidence and the bound
                  payable through three typed tools. Default analysis only
                  recommends. Explicit delegation below permits the worker to
                  prepare a version-bound decision/proposal, never approve it or
                  mutate the target. Operator-started runtime required; smoke
                  evidence is not held-out quality acceptance.
                </p>
                <label className="choice">
                  <input
                    type="checkbox"
                    checked={delegateProposal}
                    onChange={(e) => setDelegateProposal(e.target.checked)}
                  />
                  <span>Allow agent to prepare a review-only proposal</span>
                </label>
                <button
                  disabled={busy || !role("reviewer")}
                  onClick={() =>
                    act(async () => {
                      await api(
                        prefix + `/cases/${c.id}/analysis-runs`,
                        "POST",
                        { prepare_proposal: delegateProposal },
                        `"case-v${c.version}"`,
                      );
                      await refresh();
                    })
                  }
                >
                  Run local model analysis
                </button>
                <p data-testid="analysis-status">
                  {analysis?.status || "not requested"}
                  {analysis?.result?.error ? " · " + analysis.result.error : ""}
                </p>
                <p className="small">No target writes from analysis.</p>
                {analysis?.status === "prepared" && (
                  <button
                    onClick={() => go(c.proposal ? "changes" : "decision")}
                  >
                    Inspect agent-prepared outcome
                  </button>
                )}
                {analysis?.result?.recommendation && (
                  <>
                    <pre>
                      {JSON.stringify(analysis.result.recommendation, null, 2)}
                    </pre>
                    <button
                      onClick={() => {
                        const rec = analysis.result!.recommendation!;
                        setMode(rec.mode);
                        setQty(rec.quantity);
                        setReason(rec.reason);
                        go("decision");
                      }}
                    >
                      Review recommendation
                    </button>
                  </>
                )}
                <details>
                  <summary>Persisted analysis evidence</summary>
                  <pre>{JSON.stringify(c.analysis_runs || [], null, 2)}</pre>
                </details>
              </>,
            )}
            {panel(
              "Evidence inspector",
              <>
                <p className="small">
                  Select a source word to inspect its exact quote, scalar
                  offsets and parser-supplied character boxes. No
                  model-generated highlights.
                </p>
                <pre tabIndex={0} aria-label="Inspectable evidence JSON">
                  {word ? JSON.stringify(word, null, 2) : "No word selected"}
                </pre>
                <span className="mono">SHA256 {s?.sha256 || "none"}</span>
              </>,
            )}
          </div>
        </div>
      </>
    );
  } else if (route === "reconciliation" && c)
    content = (
      <div className="grid">
        <div className="stack">
          {panel(
            "Invoice, purchase order and receipt",
            <div className="tablewrap" tabIndex={0} role="region" aria-label="Invoice, purchase order and receipt comparison">
              <table>
                <thead>
                  <tr>
                    <th>Check</th>
                    <th>Invoice</th>
                    <th>Purchase order</th>
                    <th>Receipt</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td>Quantity · {displayUnit(c.unit)}</td>
                    <td>{c.invoice_quantity}</td>
                    <td>{c.ordered_quantity}</td>
                    <td>{c.received_quantity}</td>
                  </tr>
                  <tr>
                    <td>Unit price · {displayCurrency(c.currency)}</td>
                    <td>{c.unit_price}</td>
                    <td>{c.unit_price}</td>
                    <td>Not an amount source</td>
                  </tr>
                  <tr>
                    <td>Identity</td>
                    <td>{c.invoice}</td>
                    <td>Source purchase order</td>
                    <td>
                      {c.receipt_manifest?.rows
                        .map((r) => r.receipt_id)
                        .join(", ") || "Receipt identity needs review"}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>,
          )}
          <div className="ribbon">
            <strong>Quantity exception</strong>
            <p>
              Receipted quantity:{" "}
              {quantityWithUnit(c.received_quantity, c.unit)}
              {" of "}
              {quantityWithUnit(c.invoice_quantity, c.unit)}. Unsupported
              quantity must not be recognized.
            </p>
          </div>
          {panel(
            "Calculation policy v1",
            <>
              <div className="math">
                recognized quantity ≤ min(ordered, invoiced, confirmed receipt)
                <br />
                net = quantity × exact decimal unit price
                <br />
                tax = net × tax rate; per-line half-up to{" "}
                {minorUnitQuantum(c.currency)
                  ? `${displayCurrency(c.currency)} ${minorUnitQuantum(c.currency)}`
                  : "the server's pinned currency quantum (unavailable until currency is resolved)"}
                <br />
                gross = net + tax
                <br />
                retained balance = original invoice face − gross
              </div>
              <p className="small muted" style={{ marginTop: 10 }}>
                The server computes the executable patch after a reviewer saves
                a supported decision. No floating-point amount in this interface
                is authoritative.
              </p>
              <button className="primary" onClick={() => go("decision")}>
                Resolve exception
              </button>
            </>,
          )}
        </div>
        {panel(
          "Evidence & locality",
          <>
            <p>Source documents: {c.sources.length} real parser artifacts.</p>
            <h3>Receipt support & allocations</h3>
            <div data-testid="receipt-lines">
              {c.receipt_manifest?.rows.map((row) => (
                <div className="field" key={row.key}>
                  <strong>
                    {row.receipt_id} / line {row.line_id}:{" "}
                    {quantityWithUnit(row.quantity, row.unit || c.unit)} ·{" "}
                    {displayCurrency(row.currency || c.currency)}
                  </strong>
                  {row.evidence.map((e, i) => (
                    <button
                      key={i}
                      onClick={() => {
                        const index = c.sources.findIndex(
                          (s) => s.id === e.source_id,
                        );
                        if (index < 0) {
                          setError("The cited source is no longer available in this case. Refresh the case before opening its evidence.");
                          return;
                        }
                        if (!c.sources[index]?.pages.some((page) => page.number === e.page)) {
                          setError("The cited page is unavailable in the current source version. Refresh the case before opening its evidence.");
                          return;
                        }
                        setSourceIndex(index);
                        setSourcePageNumber(e.page);
                        setWord(
                          c.sources[index]?.pages
                            .find((p) => p.number === e.page)
                            ?.words.find(
                              (w) => w.start <= e.start && w.end >= e.end,
                            ) || null,
                        );
                        go("document");
                      }}
                    >
                      {c.sources.find((s) => s.id === e.source_id)?.name} · page{" "}
                      {e.page} · source v{e.source_version} · “{e.quote}”
                    </button>
                  ))}
                </div>
              ))}
            </div>
            <p className="small muted">
              Each receipt/line counts once across aliases. Prior reservations
              are rechecked transactionally when saving; source totals are not a
              promise of unallocated capacity.
            </p>
            {!!c.receipt_manifest?.errors.length && (
              <p role="status">{c.receipt_manifest.errors.join(", ")}</p>
            )}
            <h3>Missing information</h3>
            <p>
              Missing: {c.missing_fields?.join(", ") || "none"}. Conflicts:{" "}
              {c.conflicting_fields?.join(", ") || "none"}.
            </p>
            <div data-testid="information-requests">
              {c.information_requests?.map((r) => (
                <p key={r.id}>
                  {r.state} · {r.fields.join(", ")} · {r.reason}
                </p>
              ))}
            </div>
            <label htmlFor="info-reason">Information request reason</label>
            <textarea
              id="info-reason"
              value={infoReason}
              onChange={(e) => setInfoReason(e.target.value)}
            />
            <button
              disabled={busy || !role("reviewer") || !infoReason.trim()}
              onClick={() =>
                act(async () => {
                  const fields = [
                    ...new Set([
                      ...(c.missing_fields || []),
                      ...(c.conflicting_fields || []),
                    ]),
                  ];
                  if (!fields.length)
                    throw Error(
                      "No unresolved fields; inspect sources or record a hold/escalation.",
                    );
                  await api(
                    prefix + `/cases/${c.id}/information-requests`,
                    "POST",
                    { fields, reason: infoReason },
                    `"case-v${c.version}"`,
                  );
                  await refresh();
                })
              }
            >
              Request missing information
            </button>
            <button
              disabled={!role("reviewer")}
              onClick={() => {
                setAttachSupport(true);
                setLocalFiles([]);
                go("import");
              }}
            >
              Attach supporting documents
            </button>
            <button onClick={() => go("document")}>
              Inspect source evidence
            </button>
            <hr className="divider" />
            <h3>Independent target match</h3>
            <button
              disabled={busy || !role("reviewer")}
              onClick={() =>
                act(async () => {
                  await api(
                    prefix + `/cases/${c.id}/target-match`,
                    "POST",
                    selectedTarget ? { selected_target: selectedTarget } : {},
                    `"case-v${c.version}"`,
                  );
                  await refresh();
                })
              }
            >
              Look up matching payable
            </button>
            {!!c.target_candidates?.length && (
              <div className="field">
                <label htmlFor="target-choice">
                  Matching target candidates
                </label>
                <select
                  id="target-choice"
                  value={selectedTarget}
                  onChange={(e) => setSelectedTarget(e.target.value)}
                >
                  <option value="">Choose an exact target</option>
                  {c.target_candidates.map((x) => (
                    <option key={x.id} value={x.id}>
                      {x.invoice_key} · revision {x.revision} · {x.id}
                    </option>
                  ))}
                </select>
              </div>
            )}
            <p className="small muted">
              Missing or ambiguous suppliers are not silently matched. Correct
              fields, repeat lookup and select a returned candidate. No supplier
              creation.
            </p>
          </>,
        )}
      </div>
    );
  else if (route === "decision" && c)
    content = (
      <div className="equal">
        {panel(
          "Choose a supported outcome",
          <form
            onSubmit={(e) => {
              e.preventDefault();
              act(saveDecision);
            }}
          >
            {[
              [
                "partial",
                "Recognize confirmed quantity",
                "Create an exact five-field proposal for independent approval.",
              ],
              [
                "hold",
                "Hold the whole invoice",
                "Record the hold reason; no target write.",
              ],
              [
                "no_change",
                "Already correct — independently check",
                "Zero target mutations; only accepted when the independently read payable exactly matches supported recognition.",
              ],
              [
                "escalate",
                "Escalate for review",
                "Preserve evidence and request human follow-up; no target write.",
              ],
            ].map(([id, label, desc]) => (
              <label className="choice" key={id}>
                <input
                  type="radio"
                  name="mode"
                  checked={mode === id}
                  onChange={() => setMode(id)}
                />
                <span>
                  <strong>{label}</strong>
                  <small>{desc}</small>
                </span>
              </label>
            ))}
            <div className="field">
              <label htmlFor="qty">Recognized quantity</label>
              <input
                id="qty"
                inputMode="decimal"
                value={qty}
                onChange={(e) => setQty(e.target.value)}
                disabled={mode !== "partial"}
              />
              <small>
                Source receipt total:{" "}
                {quantityWithUnit(c.received_quantity, c.unit)}. Server also
                deducts prior allocations.
              </small>
            </div>
            <div className="field">
              <label htmlFor="reason">Decision reason</label>
              <textarea
                id="reason"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                required
              />
            </div>
            <button
              className="primary"
              data-testid="save-decision"
              disabled={busy || !role("reviewer")}
            >
              Save decision
            </button>
          </form>,
        )}
        {panel(
          "Decision boundaries",
          <>
            <p>
              Original invoice face:{" "}
              {amountWithCurrency(c.invoice_face_amount, c.currency)}
            </p>
            <p>
              Unresolved:{" "}
              {[
                ...(c.missing_fields || []),
                ...(c.conflicting_fields || []),
              ].join(", ") || "none"}
              . Missing support blocks recognition, not hold/escalation.
            </p>
            <p>
              Only recognized_quantity, net_amount, tax_amount, gross_amount and
              status may change.
            </p>
            <p>
              The preparer cannot approve. Source corrections invalidate
              unsealed authority. After sealing, a correction must wait for
              authoritative reconciliation.
            </p>
            <p className="muted small">
              No-change uses a separate rational-arithmetic verifier and a real
              target read. A mismatch or unavailable target cannot produce
              success.
            </p>
          </>,
        )}
      </div>
    );
  else if (route === "approvals")
    content = panel(
      "Awaiting independent authorization",
      <>
        {items
          .filter((x) => x.state === "awaiting_approval")
          .map((x) => (
            <div className="task" key={x.id}>
              <div>
                <strong>
                  {x.invoice} · {x.supplier}
                </strong>
                <p>
                  Prepared by {x.proposal?.prepared_by} · revision{" "}
                  {x.proposal?.target_revision}
                </p>
              </div>
              <div className="actions">
                <button
                  className="primary"
                  onClick={() => act(() => select(x, "changes"))}
                >
                  Review exact changes
                </button>
              </div>
            </div>
          ))}
        {!items.some((x) => x.state === "awaiting_approval") && (
          <div className="empty">
            <strong>No pending approvals</strong>
            <p>No changes are approved automatically.</p>
          </div>
        )}
      </>,
    );
  else if (route === "changes" && c)
    content = p ? (
      <>
        <div className="ribbon">
          <strong>Bound approval</strong>
          <p>
            One invoice record, five fields, target revision {p.target_revision}
            . Original face amount and bank details remain protected.
          </p>
        </div>
        <div className="grid">
          {panel(
            "Exact proposed patch",
            <>
              <div className="tablewrap" tabIndex={0} role="region" aria-label="Exact proposed patch">
                <table>
                  <thead>
                    <tr>
                      <th>Field</th>
                      <th>Before</th>
                      <th>Proposed value</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(p.patch).map(([k, value]) => (
                      <tr key={k}>
                        <td>{k}</td>
                        <td>
                          {p.before_snapshot?.[k] ||
                            v?.observation?.receipt.before_snapshot[k] ||
                            "Before snapshot unavailable — legacy test proposal"}
                        </td>
                        <td>
                          <strong>{value}</strong>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <hr className="divider" />
              <p>
                <strong>Reason:</strong> {p.reason}
              </p>
              <p className="mono">Intent SHA256 {p.digest}</p>
              {p.allocation_manifest && (
                <div data-testid="allocation-manifest">
                  <h3>Bound receipt allocations</h3>
                  <p>
                    {p.allocation_manifest.policy} · total{" "}
                    {quantityWithUnit(
                      p.allocation_manifest.total,
                      p.allocation_manifest.rows[0]?.unit || c.unit,
                    )}
                  </p>
                  {p.allocation_manifest.rows.map((row) => (
                    <p key={row.key}>
                      {row.receipt_id} / line {row.line_id}: {row.allocated} of{" "}
                      {quantityWithUnit(row.quantity, row.unit || c.unit)} ·{" "}
                      {displayCurrency(row.currency || c.currency)} ·{" "}
                      {row.evidence
                        .map(
                          (e) =>
                            `source v${e.source_version} ${e.source_sha256.slice(0, 12)} page ${e.page} “${e.quote}”`,
                        )
                        .join("; ")}
                    </p>
                  ))}
                </div>
              )}
              <p className="small muted">
                Operation {p.operation_key} · expires {p.expires}
              </p>
              <button
                className="primary"
                data-testid="approve-change"
                disabled={!role("approver") || p.state !== "pending" || busy}
                onClick={() => dialog.current?.showModal()}
              >
                Approve exact change
              </button>
              {role("approver") && (
                <>
                  <div className="field">
                    <label htmlFor="authorization-reason">
                      Authorization action reason
                    </label>
                    <textarea
                      id="authorization-reason"
                      value={reason}
                      onChange={(e) => setReason(e.target.value)}
                    />
                  </div>
                  <button
                    disabled={busy || p.state !== "pending" || !reason.trim()}
                    onClick={() =>
                      act(async () => {
                        await api(
                          prefix + `/proposals/${p.id}/approvals`,
                          "POST",
                          {
                            intent_digest: p.digest,
                            confirmation: "reject",
                            reason,
                          },
                          `"proposal-v${p.version}"`,
                        );
                        await refresh();
                        setNotice(
                          "Proposal rejected; no target job authorized.",
                        );
                      })
                    }
                  >
                    Reject exact proposal
                  </button>
                  <button
                    disabled={
                      busy ||
                      p.state !== "approved" ||
                      p.approved_by !== session.username ||
                      p.gate !== "prepared" ||
                      !reason.trim()
                    }
                    onClick={() =>
                      act(async () => {
                        await api(
                          prefix + `/approvals/${p.id}/revocations`,
                          "POST",
                          { reason },
                          `"proposal-v${p.version}"`,
                        );
                        await refresh();
                        setNotice("Approval revoked before sealing.");
                      })
                    }
                  >
                    Revoke unsealed approval
                  </button>
                </>
              )}
              {!role("approver") && (
                <p className="small muted">
                  Sign out and use the independent approver account. The server
                  denies reviewer approval.
                </p>
              )}
            </>,
          )}
          {panel(
            "Scope & currentness",
            <>
              <Badge state={p.state} />
              <div className="keyvalue">
                <span>Prepared by</span>
                <strong>{p.prepared_by}</strong>
              </div>
              <div className="keyvalue">
                <span>Invoice face</span>
                <strong>
                  {amountWithCurrency(c.invoice_face_amount, c.currency)}
                </strong>
              </div>
              <div className="keyvalue">
                <span>Retained balance</span>
                <strong data-testid="retained-balance">
                  {amountWithCurrency(
                    p.retained_balance || "not recorded in legacy test",
                    c.currency,
                  )}
                </strong>
              </div>
              <p className="small">
                Retained balance is derived from the unchanged invoice face
                value and the exact gross above. It is not a sixth patched
                field.
              </p>
              <button onClick={() => go("document")}>
                Inspect original sources
              </button>
            </>,
          )}
        </div>
      </>
    ) : (
      requireCase
    );
  else if (route === "execution" && c)
    content = (
      <div className="grid">
        {panel(
          "Durable execution checkpoints",
          <>
            <div className="status-line">
              <Badge state={c.state} />
              {c.state === "verified" && (
                <strong>Verified by independent read</strong>
              )}
            </div>
            <div className="timeline">
              {[
                [
                  "Decision saved",
                  c.proposal
                    ? "Exact immutable proposal exists"
                    : "Not prepared",
                ],
                ["Independent approval", p?.approved_by || "Not authorized"],
                ["Dispatch gate", p?.gate || "Not sealed"],
                ["Target read-back", v?.outcome || "Not checked"],
              ].map(([name, status]) => (
                <div className="event" key={name}>
                  <span className="dot">•</span>
                  <div>
                    <strong>{name}</strong>
                    <p>{status}</p>
                  </div>
                </div>
              ))}
            </div>
            <div className="actions">
              <button className="primary" onClick={() => go("records")}>
                View business record
              </button>
              <button onClick={() => go("recovery")}>Inspect recovery</button>
            </div>
          </>,
        )}
        {panel(
          "Actual job attempts",
          <>
            {c.jobs?.map((j) => (
              <div key={j.id}>
                <div className="keyvalue">
                  <span>State / fence</span>
                  <strong>
                    {j.state} / {j.fence}
                  </strong>
                </div>
                <p className="mono">{j.id}</p>
                <p className="small muted">
                  Attempt {j.attempt || "not claimed"}
                </p>
              </div>
            ))}
            {!c.jobs?.length && <p>No job accepted for this packet.</p>}
            <hr className="divider" />
            <p className="small muted">
              No green completion from a model message or HTTP response alone.
              Response loss remains uncertain until an independent target read
              succeeds.
            </p>
          </>,
        )}
      </div>
    );
  else if (route === "recovery" && c)
    content = (
      <div className="equal">
        {panel(
          "Read before retry",
          <>
            <Badge state={c.state} />
            <p style={{ marginTop: 14 }}>
              The operation key is stable across restart. Recovery reads the
              target receipt, snapshots and history; it does not issue a
              replacement write.
            </p>
            <div className="math">
              Matching receipt → independently verify
              <br />
              Unavailable / nonauthoritative → remain uncertain
              <br />
              Missing receipt → retain gate; no blind replay
              <br />
              Foreign or conflicting operation → manual investigation
            </div>
            <p className="mono">
              {p?.operation_key || "No operation accepted"}
            </p>
            <button
              onClick={() =>
                act(async () => {
                  await refresh();
                  setNotice(
                    "Latest durable case and verification loaded. Automatic expired-lease recovery is handled by the worker; no write was requested.",
                  );
                })
              }
            >
              Refresh authoritative recovery status
            </button>
            <p className="small muted">
              Operator-controlled response-drop/process-kill proof is available
              in tests/test_proof.py. Deliberate fault injection is not exposed
              to ordinary users.
            </p>
          </>,
        )}
        {panel(
          "Last independent observation",
          <pre tabIndex={0} aria-label="Inspectable evidence JSON">
            {JSON.stringify(v || { outcome: "not_checked" }, null, 2)}
          </pre>,
        )}
      </div>
    );
  else if (route === "records" && c)
    content = (
      <>
        <div
          className={"ribbon " + (v?.outcome === "matched" ? "success" : "")}
        >
          <strong>
            {v?.outcome === "matched"
              ? "Historical authorized effect verified"
              : "Not independently verified"}
          </strong>
          <p>
            {v?.current_drift
              ? "Current target drift detected. Do not overwrite."
              : "An applied receipt proves this operation, not arbitrary future target freshness."}
          </p>
        </div>
        <div className="equal">
          {panel(
            "Independent target snapshot",
            v?.observation ? (
              <div>
                {Object.entries(v.observation.receipt.after_snapshot).map(
                  ([k, value]) => (
                    <div className="keyvalue" key={k}>
                      <span>{k}</span>
                      <strong>{String(value)}</strong>
                    </div>
                  ),
                )}
              </div>
            ) : (
              <p>
                No authoritative receipt fetched. No verified values are
                invented.
              </p>
            ),
          )}
          {panel(
            "Independent checks",
            <>
              {Object.entries(v?.checks || {}).map(([name, ok]) => (
                <div className="keyvalue" key={name}>
                  <span>{name.replaceAll("_", " ")}</span>
                  <strong>{ok ? "Match" : "Mismatch"}</strong>
                </div>
              ))}
              <button onClick={() => go("evidence")}>
                Inspect evidence package
              </button>
            </>,
          )}
        </div>
      </>
    );
  else if (route === "evidence" && c)
    content = (
      <div className="grid">
        {panel(
          "Immutable evidence inventory",
          <>
            {c.sources.map((s, i) => {
              const original = originalControl(c.original_availability, s.id);
              return (
                <div className="file" key={s.id}>
                  <div className="docicon">PDF</div>
                  <div>
                    <strong>{s.name}</strong>
                    <small className="mono">{s.sha256}</small>
                    {s.original_deleted && (
                      <small>Original bytes deleted; digest retained.</small>
                    )}
                    <small
                      id={`evidence-original-${i}`}
                      data-testid="evidence-original-availability"
                      data-state={original.state}
                    >
                      {original.downloadable ? "" : original.label + ". "}
                      {original.explanation}
                    </small>
                  </div>
                  <button
                    aria-label={`Download original ${s.name}`}
                    aria-describedby={`evidence-original-${i}`}
                    disabled={busy || !original.downloadable}
                    onClick={() => act(() => downloadOriginal(s))}
                  >
                    Download original
                  </button>
                </div>
              );
            })}
            <div className="file">
              <div className="docicon">JSON</div>
              <div>
                <strong>Proposal, authority and independent checks</strong>
                <small>
                  Current durable case projection; original binaries excluded
                </small>
              </div>
            </div>
            <EvidenceExport
              key={`${session.username}:${session.workspace}:${c.id}`}
              workspace={session.workspace}
              username={session.username}
              caseId={c.id}
              version={c.version}
              roles={session.roles}
              csrf={session.csrf}
            />
          </>,
        )}
        {panel(
          "JSON inspector",
          <pre tabIndex={0} aria-label="Inspectable evidence JSON">
            {JSON.stringify(
              { source_digest: c.source_digest, proposal: p, verification: v },
              null,
              2,
            )}
          </pre>,
        )}
      </div>
    );
  else if (route === "activity")
    content = panel(
      "Actual committed event history",
      <>
        <div className="tabs">
          {["all", "human", "system"].map((t) => (
            <button
              className={t === actorFilter ? "active" : ""}
              key={t}
              type="button"
              aria-pressed={t === actorFilter}
              onClick={() => setActorFilter(t)}
            >
              {t}
            </button>
          ))}
        </div>
        {events.map((e) => (
          <div className="auditrow" key={e.sequence}>
            <span className="mono">#{e.sequence}</span>
            <div>
              <strong>{e.kind}</strong>
              <p>
                {e.actor} · {e.at}
              </p>
              <p className="mono">{JSON.stringify(e.body)}</p>
            </div>
            <span>{e.actor === "system" ? "System" : "Human"}</span>
          </div>
        ))}
        {!events.length && <p>No events in this filter.</p>}
        <p className="small muted">
          Latest 200 persisted audit entries. Durable SSE reconnect/replay is
          available, with polling fallback. Complete audit chaining and
          retention-cursor expiry remain release gates.
        </p>
      </>,
    );
  else if (route === "settings")
    content = (
      <div className="equal">
        {panel(
          "Workspace access",
          <>
            <button
              disabled={busy}
              onClick={() => act(async () => setAccessSettings(await api(prefix + "/settings")))}
            >
              Refresh access and review state
            </button>
            {error.includes("STALE_VERSION") && (
              <p className="ribbon error" role="alert">
                Access state changed elsewhere. Reload the current accounts and request versions before trying again.
              </p>
            )}
            <div className="keyvalue">
              <span>Signed-in identity</span>
              <strong>{session.username}</strong>
            </div>
            <div className="keyvalue">
              <span>Current roles</span>
              <strong>{session.roles.join(", ")}</strong>
            </div>
            <div className="tablewrap" tabIndex={0} role="region" aria-label="Role authorities">
              <table>
                <thead>
                  <tr>
                    <th>Role</th>
                    <th>Authority</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td>Reviewer</td>
                    <td>Import, correct, decide</td>
                  </tr>
                  <tr>
                    <td>Approver</td>
                    <td>Approve another preparer only</td>
                  </tr>
                  <tr>
                    <td>Viewer / Auditor</td>
                    <td>Read / permitted evidence export</td>
                  </tr>
                  <tr>
                    <td>Admin</td>
                    <td>No implicit approval authority</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p className="small muted">
              Roles come from the signed-in server session. Admin access is not
              approval authority. Account changes take effect only after a
              distinct authorized reviewer approves them. A current approver
              may review an approver-role grant; other account changes require
              an administrator reviewer.
            </p>
            {role("admin") && accessSettings && (
              <>
                <h3>Request an account change</h3>
                <p className="small muted">
                  Provision new local identities through the documented operator
                  process. This screen never creates passwords or prints credentials.
                </p>
                <div className="field">
                  <label htmlFor="access-target">Account</label>
                  <select id="access-target" value={accessTarget} onChange={(e) => setAccessTarget(e.target.value)}>
                    <option value="">Choose an account</option>
                    {accessSettings.members.filter((member) => member.username !== session.username).map((member) => (
                      <option key={member.username} value={member.username}>{member.username} · {member.active ? member.roles.join(", ") || "no roles" : "inactive"}</option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label htmlFor="access-action">Change</label>
                  <select id="access-action" value={accessAction} onChange={(e) => setAccessAction(e.target.value)}>
                    <option value="grant_role">Grant role</option>
                    <option value="revoke_role">Revoke role</option>
                    <option value="activate">Activate account</option>
                    <option value="deactivate">Deactivate account</option>
                  </select>
                </div>
                {["grant_role", "revoke_role"].includes(accessAction) && (
                  <div className="field">
                    <label htmlFor="access-role">Role</label>
                    <select id="access-role" value={accessRole} onChange={(e) => setAccessRole(e.target.value)}>
                      {[
                        ["reviewer", "Reviewer · intake and decisions"],
                        ["approver", "Approver · independent decision review"],
                        ["viewer", "Viewer · permitted reads"],
                        ["auditor", "Auditor · history and redacted exports"],
                        ["admin", "Admin · account and workspace settings"],
                      ].map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                    </select>
                  </div>
                )}
                <div className="field">
                  <label htmlFor="access-reason">Reason for request</label>
                  <textarea id="access-reason" value={accessReason} onChange={(e) => setAccessReason(e.target.value)} />
                </div>
                <button disabled={busy || !accessTarget || !accessReason.trim()} onClick={() => act(submitAccessRequest)}>Submit for independent review</button>
                <h3>Account directory</h3>
                {accessSettings.members.map((member) => (
                  <div className="keyvalue" key={member.username}>
                    <span>{member.username}</span>
                    <strong>{member.active ? (member.roles.join(", ") || "No roles") : "Inactive"}</strong>
                  </div>
                ))}
              </>
            )}
            {(role("admin") || role("approver")) && accessSettings && (
              <>
                <h3>Access request history</h3>
                {accessSettings.requests
                  .filter((request) => role("admin") || (request.action === "grant_role" && request.role === "approver"))
                  .map((request) => (
                  <div className="panel" key={request.id}>
                    <div className="body">
                      <div className="keyvalue"><span>Request</span><strong>{request.action.replaceAll("_", " ")} {request.role || "account"}</strong></div>
                      <div className="keyvalue"><span>Account</span><strong>{request.target_username}</strong></div>
                      <div className="keyvalue"><span>Requested by</span><strong>{request.requested_by}</strong></div>
                      <div className="keyvalue"><span>Status</span><strong>{request.status === "approved" ? (request.action === "revoke_role" ? "revoked" : request.action === "grant_role" ? "granted" : request.action === "deactivate" ? "deactivated" : "activated") : request.status}</strong></div>
                      <p>{request.reason}</p>
                      {request.review_reason && <p>Review note: {request.review_reason}</p>}
                      {request.status === "pending" && (request.action === "grant_role" && request.role === "approver" ? role("approver") : role("admin")) && (
                        <>
                          <label htmlFor={`review-reason-${request.id}`}>Independent review reason</label>
                          <textarea id={`review-reason-${request.id}`} value={accessReviewReason} onChange={(e) => setAccessReviewReason(e.target.value)} />
                          <button disabled={busy || !accessReviewReason.trim() || request.requested_by === session.username || request.target_username === session.username} onClick={() => act(() => decideAccessRequest(request, "approve"))}>Approve access change</button>
                          <button disabled={busy || !accessReviewReason.trim() || request.requested_by === session.username || request.target_username === session.username} onClick={() => act(() => decideAccessRequest(request, "deny"))}>Deny request</button>
                        </>
                      )}
                    </div>
                  </div>
                ))}
                {!accessSettings.requests.some((request) => role("admin") || (request.action === "grant_role" && request.role === "approver")) && <p>No access requests are available for your roles.</p>}
              </>
            )}
          </>,
        )}
        {panel(
          "Locality & retention",
          <>
            <Badge state="external model egress disabled" />
            <p style={{ marginTop: 12 }}>
              No hosted provider calls or paid APIs. Synthetic originals remain
              under fixtures/public; database state is PostgreSQL.
            </p>
            <div className="field">
              <label htmlFor="retention">
                Source retention policy (not enforced)
              </label>
              <select id="retention" disabled defaultValue="90">
                <option value="90">90 days proposed</option>
              </select>
            </div>
            <p className="small muted">
              Uploaded original bytes expire after 90 days unless held; decision
              text is retained separately. The full 365-day decision purge and
              encrypted-backup expiry gate remain open. Do not import real
              private documents.
            </p>
            {c && (
              <>
                <h3>Selected packet retention</h3>
                <p>
                  Legal hold:{" "}
                  {c.retention?.legal_hold ? "active" : "not active"}.{" "}
                  {c.retention?.originals_deleted_at
                    ? "Stored original bytes deleted; digest tombstones retained. "
                    : ""}
                  {storedOriginalCount(c.original_availability) === null
                    ? "Stored original availability was not reported by this server; deletion is withheld."
                    : `${storedOriginalCount(c.original_availability)} source(s) currently have stored original bytes.`}
                </p>
                <div className="field">
                  <label htmlFor="retention-reason">
                    Retention action reason
                  </label>
                  <textarea
                    id="retention-reason"
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                  />
                </div>
                <button
                  disabled={busy || !role("admin") || !reason.trim()}
                  onClick={() =>
                    act(async () => {
                      await api(
                        prefix + `/cases/${c.id}/retention-holds`,
                        "POST",
                        { enabled: !c.retention?.legal_hold, reason },
                        `"case-v${c.version}"`,
                      );
                      await refresh();
                    })
                  }
                >
                  {c.retention?.legal_hold
                    ? "Release legal hold"
                    : "Place legal hold"}
                </button>
                <button
                  disabled={
                    busy ||
                    !role("admin") ||
                    !storedOriginalCount(c.original_availability) ||
                    !reason.trim() ||
                    !!c.retention?.legal_hold
                  }
                  onClick={() =>
                    act(async () => {
                      if (
                        !window.confirm(
                          "Delete stored original bytes? Decision text and backup copies have separate retention; this does not erase target history.",
                        )
                      )
                        return;
                      await api(
                        prefix + `/cases/${c.id}/original-deletions`,
                        "POST",
                        { reason },
                        `"case-v${c.version}"`,
                      );
                      await refresh();
                      setNotice(
                        "Stored original bytes deleted; digest tombstones and decision evidence retained. Backup expiry remains separately disclosed.",
                      );
                    })
                  }
                >
                  Delete uploaded original bytes
                </button>
              </>
            )}
          </>,
        )}
      </div>
    );
  else content = requireCase;
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <svg viewBox="0 0 28 28" width="28" height="28">
            <path d="M5 23V5h18M5 12h15M5 19h10" strokeWidth="3" />
          </svg>
          <span>folio</span>
        </div>
        <div className="workspace">
          <span className="avatar">W</span>
          <span>
            <strong>{currentWorkspace}</strong>
            <small>Signed-in workspace</small>
          </span>
        </div>
        <nav aria-label="Workspace navigation">
          {screens.map(([id, name], i) => (
            <React.Fragment key={id}>
              {[0, 3, 6, 12].includes(i) && (
                <div className="navlabel">
                  {i === 0
                    ? "Workspace"
                    : i === 3
                      ? "Review"
                      : i === 6
                        ? "Operations"
                        : "Workspace"}
                </div>
              )}
              <button
                className={"navitem " + (route === id ? "active" : "")}
                aria-current={route === id ? "page" : undefined}
                onClick={() => go(id)}
              >
                {name}
              </button>
            </React.Fragment>
          ))}
        </nav>
        <div className="sidebarfooter">
          <strong>{session.username}</strong>
          <small>{session.roles.join(" · ")}</small>
        </div>
      </aside>
      <div className="app">
        <header className="topbar">
          <span>{currentWorkspace}</span>
          <span>/</span>
          <strong>Document operations</strong>
          <span className="spacer" />
          <span className="small" role="status">
            Events: {streamStatus}
          </span>
          <button
            disabled={busy || !!pendingMutation || needsMutationReconciliation}
            title={
              pendingMutation || needsMutationReconciliation
                ? "Resolve the pending request before signing out."
                : undefined
            }
            onClick={() => act(signout)}
          >
            Sign out
          </button>
          <span className="avatar">
            {session.username.slice(0, 2).toUpperCase()}
          </span>
        </header>
        <div className="mobile-nav">
          <label htmlFor="mobile-route" className="small">
            Workspace navigation
          </label>
          <select
            id="mobile-route"
            value={route}
            onChange={(e) => go(e.target.value)}
          >
            {screens.map(([id, name]) => (
              <option value={id} key={id}>
                {name}
              </option>
            ))}
          </select>
        </div>
        <main id="screen" data-current-screen={route}>
          <div className="pagehead">
            <div>
              <div className="eyebrow">
                {c ? c.label + " / " + c.invoice : "Document operations"}
              </div>
              <h1 ref={heading} tabIndex={-1}>
                {title}
              </h1>
              <p>
                {c
                  ? c.supplier +
                    " · " +
                    displayCurrency(c.currency) +
                    " · durable revision " +
                    c.version
                  : "Review evidence, authorize exact changes, verify independently."}
              </p>
            </div>
            <div className="actions">
              {route === "overview" && (
                <button className="primary" onClick={() => go("import")}>
                  Import documents
                </button>
              )}
              {route !== "overview" && c && <Badge state={c.state} />}
            </div>
          </div>
          {(error || durabilityProblem || pendingMutation || needsMutationReconciliation) && (
            <div className="ribbon error" role="alert">
              <strong>
                {durabilityProblem
                  ? "Safe command storage unavailable"
                  : pendingMutation
                  ? "Request outcome unknown"
                  : needsMutationReconciliation
                    ? "Reconciliation required"
                    : "Action not completed"}
              </strong>
              {durabilityProblem && !pendingMutation ? (
                <p>{durabilityProblem}</p>
              ) : pendingMutation ? (
                <p>
                  {pendingMutation.durableOnly
                    ? "Folio did not return a readable response before reload. Only the opaque operation key remains; check its server receipt or review authorized records and activity before acknowledging the unresolved outcome."
                    : "Folio did not return a readable response. The original change may have completed. No different change will be sent until you retry the exact request with its original idempotency key and version."}
                </p>
              ) : needsMutationReconciliation ? (
                <p>
                  The safe replay window ended while a request was unresolved.
                  Review current workspace records and activity before
                  explicitly allowing new changes.
                </p>
              ) : (
                <p>{error}</p>
              )}
              {durabilityProblem && !pendingMutation ? null : pendingMutation ? (
                <>
                  {pendingMutation.durableOnly ? (
                      <button disabled={busy} onClick={() => void lookupReloadedMutation()}>
                      Check server receipt
                    </button>
                  ) : (
                    <>
                      <button disabled={busy} onClick={() => void retryUncertainMutation()}>
                        Retry exact request
                      </button>
                      <button disabled={busy} onClick={() => void lookupReloadedMutation()}>
                        Check server receipt
                      </button>
                    </>
                  )}
                  <button disabled={busy} onClick={() => act(refresh)}>
                    Refresh workspace state
                  </button>
                  <button disabled={busy} onClick={acknowledgeReconciliation}>
                    I reviewed audit history and acknowledge the unresolved request
                  </button>
                </>
              ) : needsMutationReconciliation ? (
                <button onClick={acknowledgeReconciliation}>
                  I reconciled the request
                </button>
              ) : (
                <button onClick={() => act(refresh)}>Retry refresh</button>
              )}
            </div>
          )}
          {notice && (
            <div className="ribbon success" role="status">
              {notice}
            </div>
          )}
          {busy && (
            <p role="status">Waiting for a confirmed server response…</p>
          )}
          {content}
          <p className="note" style={{ marginTop: 24 }}>
            Local engineering build · authored synthetic documents only · no
            payment or bank changes · release gates remain open
          </p>
        </main>
      </div>
      <dialog ref={dialog} aria-labelledby="approval-title">
        <div className="body">
          <h2 id="approval-title">Authorize this exact change?</h2>
          <p>
            Approve only the five displayed fields for {c?.invoice} at target
            revision {p?.target_revision}. Sealing is the irreversible
            authorization point; later cancellation cannot promise no effect.
          </p>
          <p className="mono">{p?.digest}</p>
          <div className="actions">
            <button onClick={() => dialog.current?.close()}>Cancel</button>
            <button
              className="primary"
              data-testid="confirm-approval"
              disabled={busy}
              onClick={() => act(approve)}
            >
              Confirm independent approval
            </button>
          </div>
          {error && <p role="alert">{error}</p>}
          {(pendingMutation || needsMutationReconciliation) && (
            <>
              {pendingMutation && (
                <>
                  <p role="status">
                    The original request may have completed. Retry it with the
                    same key and version, or reconcile current records and
                    activity.
                  </p>
                  <button
                    disabled={busy}
                    onClick={() => void retryUncertainMutation()}
                  >
                    Retry exact request
                  </button>
                </>
              )}
              <button disabled={busy} onClick={acknowledgeReconciliation}>
                I reconciled the request
              </button>
            </>
          )}
        </div>
      </dialog>
    </div>
  );
}
createRoot(document.getElementById("root")!).render(<App />);
