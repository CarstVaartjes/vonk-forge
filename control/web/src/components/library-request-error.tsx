import {safeErrorText} from "../lib/error-display";

/** Request exceptions are local display state, not persisted availability evidence. */
export function LibraryRequestError({error, title, onRetry, retryLabel = "Retry"}: {
  error: unknown;
  title: string;
  onRetry?(): void;
  retryLabel?: string;
}) {
  const message = error instanceof Error ? error.message : typeof error === "string" ? error : "";
  const detail = safeErrorText(message);
  return <section className="library-availability-feedback" role="alert" aria-label={title}>
    <strong>{title}</strong>
    {detail && <p>{detail}</p>}
    {onRetry && <button type="button" className="button secondary" onClick={onRetry}>{retryLabel}</button>}
  </section>;
}
