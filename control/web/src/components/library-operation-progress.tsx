import {useEffect, useState} from "react";
import type {JobDetail, LibraryApi, LibraryOperation} from "../api/types";
import type {LibraryActionName} from "./library-action-types";

const COMPLETE_STATES = new Set(["succeeded", "complete", "completed"]);
const INCOMPLETE_STATES = new Set(["partial", "failed", "cancelled", "canceled", "lost"]);

export function operationSettled(state: string): boolean {
  return COMPLETE_STATES.has(state) || INCOMPLETE_STATES.has(state);
}

function errorMessage(value: unknown): string {
  return (value instanceof Error ? value.message : "Unable to read operation authority.").slice(0, 256);
}

function jobId(operation: LibraryOperation): string | undefined {
  const value = operation.result?.job_id;
  return typeof value === "string" ? value : undefined;
}

export function LibraryOperationProgress({api, name, onChange, onRefresh, operation}: {
  api: LibraryApi;
  name: LibraryActionName;
  onChange(operation: LibraryOperation): void;
  onRefresh(): Promise<void>;
  operation: LibraryOperation;
}) {
  const [job, setJob] = useState<JobDetail>();
  const [error, setError] = useState("");
  const [pollAttempt, setPollAttempt] = useState(0);
  const [retrying, setRetrying] = useState(false);

  useEffect(() => {
    if (operationSettled(operation.state)) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const next = await api.libraryOperation(operation.id);
        if (!active) return;
        setError("");
        onChange(next);
        const authoritativeJobId = jobId(next);
        if (authoritativeJobId) {
          try {
            const progress = await api.libraryJobProgress(authoritativeJobId);
            if (active) setJob(progress);
          } catch (value) {
            if (active) setError(errorMessage(value));
          }
        }
        if (active && !operationSettled(next.state)) timer = setTimeout(poll, 1000);
        if (active && operationSettled(next.state)) await onRefresh();
      } catch (value) {
        if (active) setError(errorMessage(value));
      }
    };
    void poll();
    return () => {
      active = false;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [api, onChange, onRefresh, operation.id, operation.state, pollAttempt]);

  async function retry() {
    setRetrying(true);
    setError("");
    try {
      const next = await api.retryLibraryOperation(operation.id);
      setJob(undefined);
      onChange(next);
    } catch (value) {
      setError(errorMessage(value));
    } finally {
      setRetrying(false);
    }
  }

  const complete = COMPLETE_STATES.has(operation.state);
  const incomplete = INCOMPLETE_STATES.has(operation.state);
  return <section className={`library-operation operation-${operation.state}`} role="region" aria-label={`${name} operation progress`}>
    <div aria-live="polite" aria-atomic="true">
      <strong>{complete ? "Operation complete" : incomplete ? "Operation incomplete" : "Operation in progress"}</strong>
      <span>{operation.kind} · {operation.state}</span>
    </div>
    <p>{operation.nodes.join(" + ")}</p>
    {job && <p>{job.progress.completed} of {job.progress.total} ranks completed · {job.progress.failed} failed</p>}
    {error && <div role="alert"><p>{error}</p>{!incomplete && <button type="button" onClick={() => setPollAttempt(value => value + 1)}>Retry status</button>}</div>}
    {incomplete && <button type="button" onClick={() => void retry()} disabled={retrying}>{retrying ? "Retrying…" : "Retry incomplete operation"}</button>}
  </section>;
}
