import {useEffect, useMemo, useState} from "react";
import type {AvailabilityOperationFailure} from "../api/types";
import {safeErrorText} from "../lib/error-display";

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

type RecoveryAction = NonNullable<AvailabilityOperationFailure["recovery_actions"]>[number];
const RECOVERY_ACTION_LABELS: Record<RecoveryAction, string> = {
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

/** Adapt canonical failure evidence to display text; operation context stays explicit. */
export function availabilityFailure(value: AvailabilityOperationFailure, context: {operationId?: string; preservedBytes?: number} = {}): AvailabilityFailure {
  const recoveryCodes = value.recovery_actions ?? [];
  return {
    code: safeErrorText(value.code, 128),
    detail: safeErrorText(value.detail),
    recovery: recoveryCodes.map(action => RECOVERY_ACTION_LABELS[action]),
    recoveryCodes,
    retryAt: value.retry_time ?? undefined,
    retryAfterSeconds: value.retry_after_seconds ?? undefined,
    operationId: context.operationId ? safeErrorText(context.operationId, 128) : undefined,
    preserved: context.preservedBytes !== undefined && context.preservedBytes > 0 ? `${context.preservedBytes} bytes of progress retained.` : undefined,
    logExcerpt: value.log_excerpt ? safeErrorText(value.log_excerpt, 2_000) : undefined,
    requiredBytes: value.required_bytes ?? undefined,
    freeBytes: value.free_bytes ?? undefined,
    shortfallBytes: value.shortfall_bytes ?? undefined,
  };
}

export function availabilityRetryable(value: AvailabilityOperationFailure | null | undefined): boolean {
  return value?.retryable === true;
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
