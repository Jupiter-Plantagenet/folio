// Per-source original availability reported by the case detail response.
// Advisory only: the download route re-authorizes and re-hashes the bytes. A
// response without this exact schema (for example an older API) never enables
// a download, and the case-level intake marker is never used as a substitute.
export type OriginalAvailabilityEntry = {
  source_id: string;
  source_version: number | null;
  state: string;
  code: string | null;
  downloadable: boolean;
  bytes_stored: boolean;
  retention_expired: boolean | null;
};
export type OriginalAvailability = {
  schema_version: string;
  advisory: boolean;
  download_permitted: boolean;
  legal_hold: boolean;
  sources: OriginalAvailabilityEntry[];
};
export type OriginalControl = {
  state: string;
  downloadable: boolean;
  label: string;
  explanation: string;
};

const SCHEMA = "folio-original-availability-v1";

const UNAVAILABLE: Record<string, [string, string]> = {
  not_stored: [
    "Original not stored",
    "No original bytes were stored for this source (authored sample fixture). Its digest and parsed text remain inspectable.",
  ],
  missing: [
    "Original missing",
    "This uploaded source has no stored original object. Nothing is reconstructed; report a storage fault.",
  ],
  bytes_missing: [
    "Original bytes missing",
    "The stored original has no bytes and no deletion tombstone. Nothing is reconstructed; report a storage fault.",
  ],
  deleted: [
    "Original bytes deleted",
    "Original bytes were deleted by retention or an authorized deletion. The digest tombstone remains.",
  ],
  too_large: [
    "Original too large",
    "The stored original exceeds the 20 MiB download limit and is not served.",
  ],
  metadata_mismatch: [
    "Original unavailable",
    "Source metadata does not identify exactly one stored original. Download is withheld pending integrity review.",
  ],
  digest_mismatch: [
    "Integrity check failed",
    "Stored bytes do not match the recorded SHA-256. Download is withheld; nothing is served or reconstructed.",
  ],
};

export function readAvailability(value: unknown): OriginalAvailability | null {
  if (!value || typeof value !== "object") return null;
  const report = value as Partial<OriginalAvailability>;
  if (report.schema_version !== SCHEMA || !Array.isArray(report.sources))
    return null;
  return report as OriginalAvailability;
}

export function originalControl(
  value: unknown,
  sourceId: string,
): OriginalControl {
  const report = readAvailability(value);
  if (!report)
    return {
      state: "unreported",
      downloadable: false,
      label: "Original availability unknown",
      explanation:
        "This server did not report per-source original availability, so no download is offered. Refresh after the API is updated.",
    };
  const matches = report.sources.filter(
    (entry) => entry && entry.source_id === sourceId,
  );
  if (matches.length !== 1)
    return {
      state: "unreported",
      downloadable: false,
      label: "Original availability unknown",
      explanation:
        "No single availability record was reported for this source, so no download is offered.",
    };
  const entry = matches[0];
  if (report.download_permitted !== true)
    return {
      state: "not_permitted",
      downloadable: false,
      label: "Original download not permitted",
      explanation:
        "Your role cannot download original files. Digests and parsed text remain inspectable; ask a reviewer or approver.",
    };
  if (
    entry.state === "available" &&
    entry.downloadable === true &&
    entry.bytes_stored === true &&
    entry.code === null
  ) {
    const retention =
      entry.retention_expired === true
        ? report.legal_hold === true
          ? " Past the 90-day retention period; legal hold suspends deletion."
          : " Past the 90-day retention period; scheduled deletion is pending."
        : report.legal_hold === true
          ? " Legal hold active: retention deletion is suspended."
          : "";
    return {
      state: "available",
      downloadable: true,
      label: "Download original",
      explanation:
        "The stored original's recorded SHA-256 matches this source; the server re-hashes the bytes at download." +
        retention,
    };
  }
  const [label, explanation] = UNAVAILABLE[entry.state] || [
    "Original unavailable",
    "The server reported an unrecognized availability state, so no download is offered.",
  ];
  return { state: entry.state, downloadable: false, label, explanation };
}

// Count of sources with stored bytes; null when the server did not report it.
export function storedOriginalCount(value: unknown): number | null {
  const report = readAvailability(value);
  if (!report) return null;
  return report.sources.filter((entry) => entry && entry.bytes_stored === true)
    .length;
}

export function downloadProblem(detail: unknown): string {
  switch (detail) {
    case "ORIGINAL_DELETED":
      return "These original bytes have been deleted. The digest tombstone remains available.";
    case "ORIGINAL_BYTES_MISSING":
      return "The stored original has no bytes and no deletion tombstone. Nothing was downloaded; report a storage fault.";
    case "ORIGINAL_DIGEST_MISMATCH":
      return "The stored bytes failed their SHA-256 check at download. Nothing was downloaded.";
    case "ORIGINAL_METADATA_MISMATCH":
      return "Source metadata no longer identifies this stored original. Nothing was downloaded.";
    case "ORIGINAL_EXPORT_DENIED":
      return "Your role cannot download original files. Nothing was downloaded.";
    case "ORIGINAL_TOO_LARGE":
      return "The stored original exceeds the 20 MiB download limit. Nothing was downloaded.";
    case "NOT_FOUND":
    case "NOT_FOUND_OR_FORBIDDEN":
      return "No stored original is available to this account for this source.";
    default:
      return typeof detail === "string"
        ? detail
        : "The original could not be downloaded. Refresh and retry.";
  }
}
