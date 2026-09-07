import {useEffect, useMemo, useState} from "react";

export type AvailabilityFailure = {
  code: string;
  detail: string;
  recovery: string[];
  recoveryCodes: string[];
  retryAt?: string;
  retryAfterSeconds?: number;
  operationId?: string;
  preserved?: string;
  logExcerpt?: string;
  requiredBytes?: number;
  freeBytes?: number;
  shortfallBytes?: number;
};

const MAX_TEXT = 512;
const MAX_LOG = 2_000;
const RECOVERY_ACTION_LABELS: Record<string, string> = {
  retry: "Retry the operation",
  resume: "Resume the operation",
  download_again: "Download the exact selected bytes again",
  force_rebuild: "Rebuild the exact selected Recipe image",
  open_model_access: "Open the selected Model access page",
  configure_hf_token: "Configure the existing protected HF token file",
  check_access_and_resume: "Check access and resume",
  free_space: "Free NAS space, then retry",
  inspect: "Inspect the operation details",
};

function boundedText(value: unknown, fallback: string, maximum = MAX_TEXT): string {
  if (typeof value !== "string" || !value.trim()) return fallback;
  const text = value.replace(/\u0000/g, "").trim();
  return text.length > maximum ? `${text.slice(0, maximum - 15)}…<truncated>` : text;
}

function safeLog(value: unknown): string | undefined {
  if (typeof value !== "string" || !value.trim()) return undefined;
  const redacted = value
    .replace(/(authorization\s*:\s*(?:bearer|basic)\s+)[^\s,;]+/gim, "$1<redacted>")
    .replace(/(token|api[_-]?key|secret|password)\s*[:=]\s*[^\s,;]+/gim, "$1=<redacted>")
    .replace(/https?:\/\/[^\s?]+\?[^\s]+/g, "<signed-url-redacted>");
  return boundedText(redacted, "", MAX_LOG) || undefined;
}

function objectValue(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
}

function recoveryList(value: unknown): {codes: string[]; labels: string[]} {
  const values = Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string" && Boolean(item.trim()))
    : typeof value === "string" && value.trim() ? [value] : [];
  return {codes: values, labels: values.map(item => RECOVERY_ACTION_LABELS[item] ?? "Review the operation recovery guidance")};
}

/**
 * Read the common availability failure shape without duplicating an API DTO.
 * Generated API types can be passed directly; this also keeps older string
 * errors actionable until the Controller contract is upgraded.
 */
export function availabilityFailure(value: unknown, fallbackDetail = "The availability operation could not complete."): AvailabilityFailure {
  const root = objectValue(value);
  const nested = objectValue(root?.failure) ?? root;
  const code = boundedText(nested?.code, "availability.operation_failed", 128);
  const detail = boundedText(nested?.detail ?? (typeof value === "string" ? value : undefined), fallbackDetail);
  const retryAfter = nested?.retry_after_seconds;
  const retryAfterSeconds = typeof retryAfter === "number" && Number.isFinite(retryAfter) && retryAfter >= 0
    ? Math.min(86_400, Math.floor(retryAfter))
    : undefined;
  const retryAt = typeof nested?.retry_time === "string"
    ? boundedText(nested.retry_time, "")
    : undefined;
  const progress = objectValue(nested?.progress) ?? objectValue(root?.progress);
  const recovery = recoveryList(nested?.recovery_actions);
  const progressBytes = progress?.completed_bytes;
  const preserved = boundedText(progress?.preserved, "", 256)
    || (typeof progressBytes === "number" && progressBytes > 0 ? `${progressBytes} bytes of progress retained.` : undefined);
  const operationId = boundedText(root?.id, "", 128) || undefined;
  const logExcerpt = safeLog(nested?.log_excerpt ?? nested?.log ?? nested?.logs);
  const bytes = (key: string): number | undefined => {
    const raw = nested?.[key];
    return typeof raw === "number" && Number.isFinite(raw) && raw >= 0 ? raw : undefined;
  };
  return {code, detail, recovery: recovery.labels, recoveryCodes: recovery.codes, retryAt, retryAfterSeconds, operationId, preserved, logExcerpt, requiredBytes: bytes("required_bytes"), freeBytes: bytes("free_bytes"), shortfallBytes: bytes("shortfall_bytes")};
}

/** Return the shared retry decision without exposing the API DTO in UI code. */
export function availabilityRetryable(value: unknown): boolean {
  const root = objectValue(value);
  const nested = objectValue(root?.failure) ?? root;
  return nested?.retryable === true;
}

function retryLabel(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return remainder ? `${minutes}m ${remainder}s` : `${minutes}m`;
}

export function LibraryAvailabilityFeedback({failure, modelAccessUrl, onCheckAccessAndResume, onRetry, retryLabel: actionLabel = "Retry", title = "Availability needs attention"}: {
  failure: AvailabilityFailure;
  modelAccessUrl?: string;
  onCheckAccessAndResume?: () => void;
  onRetry?: () => void;
  retryLabel?: string;
  title?: string;
}) {
  const [remaining, setRemaining] = useState(failure.retryAfterSeconds);
  useEffect(() => {
    setRemaining(failure.retryAfterSeconds);
    if (failure.retryAfterSeconds === undefined || failure.retryAfterSeconds <= 0) return;
    const timer = window.setInterval(() => setRemaining(value => value === undefined || value <= 0 ? 0 : value - 1), 1_000);
    return () => window.clearInterval(timer);
  }, [failure.retryAfterSeconds]);
  const retryDisabled = remaining !== undefined && remaining > 0;
  const retryText = useMemo(() => remaining === undefined ? actionLabel : retryDisabled ? `Retry in ${retryLabel(remaining)}` : actionLabel, [actionLabel, remaining, retryDisabled]);
  return <section className="library-availability-feedback" role="alert" aria-label={title}>
    <strong>{title}</strong>
    <p>{failure.detail}</p>
    {failure.preserved && <p className="library-availability-preserved"><strong>Preserved:</strong> {failure.preserved}</p>}
    {failure.retryAt && <p className="library-availability-retry-time">Next retry: <time dateTime={failure.retryAt}>{failure.retryAt}</time></p>}
    {(failure.requiredBytes !== undefined || failure.freeBytes !== undefined || failure.shortfallBytes !== undefined) && <dl className="library-availability-capacity"><div><dt>Required</dt><dd>{failure.requiredBytes ?? "Unknown"} bytes</dd></div><div><dt>Free</dt><dd>{failure.freeBytes ?? "Unknown"} bytes</dd></div>{failure.shortfallBytes !== undefined && <div><dt>Shortfall</dt><dd>{failure.shortfallBytes} bytes</dd></div>}</dl>}
    {failure.recovery.length > 0 && <ul aria-label="Recovery steps">{failure.recovery.map((step, index) => <li key={`${index}-${step}`}>{step}{failure.recoveryCodes[index] === "open_model_access" && modelAccessUrl && <> · <a href={modelAccessUrl} target="_blank" rel="noreferrer">Open Model access page</a></>}</li>)}</ul>}
    {failure.recoveryCodes.includes("configure_hf_token") && <p className="library-availability-token-help">Use the existing protected HF token secret file configured for the Controller. Tokens are never entered or displayed here.</p>}
    <div className="library-availability-actions">{onRetry && <button type="button" className="button secondary" disabled={retryDisabled} onClick={onRetry}>{retryText}</button>}{onCheckAccessAndResume && failure.recoveryCodes.includes("check_access_and_resume") && <button type="button" className="button secondary" onClick={onCheckAccessAndResume}>Check access and resume</button>}<details><summary>Technical details</summary><dl><div><dt>Code</dt><dd>{failure.code}</dd></div>{failure.operationId && <div><dt>Operation</dt><dd>{failure.operationId}</dd></div>}</dl>{failure.logExcerpt && <pre>{failure.logExcerpt}</pre>}</details></div>
  </section>;
}
