import {useEffect, useRef, useState} from "react";
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
  const mounted = useRef(false);
  const retryController = useRef<AbortController | undefined>(undefined);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      retryController.current?.abort();
    };
  }, []);

  useEffect(() => {
    if (operationSettled(operation.state)) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const next = await api.libraryOperation(operation.id, controller.signal);
        if (controller.signal.aborted) return;
        setError("");
        const authoritativeJobId = jobId(next);
        if (authoritativeJobId) {
          try {
            const progress = await api.libraryJobProgress(authoritativeJobId, controller.signal);
            if (!controller.signal.aborted) setJob(progress);
          } catch (value) {
            if (!controller.signal.aborted) setError(errorMessage(value));
          }
        }
        if (controller.signal.aborted) return;
        if (operationSettled(next.state)) {
          await onRefresh();
          if (controller.signal.aborted) return;
          onChange(next);
          return;
        }
        onChange(next);
        if (!controller.signal.aborted) timer = setTimeout(poll, 1000);
      } catch (value) {
        if (!controller.signal.aborted) setError(errorMessage(value));
      }
    };
    void poll();
    return () => {
      controller.abort();
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [api, onChange, onRefresh, operation.id, operation.state, pollAttempt]);

  async function retry() {
    const controller = new AbortController();
    retryController.current?.abort();
    retryController.current = controller;
    setRetrying(true);
    setError("");
    try {
      const next = await api.retryLibraryOperation(operation.id, controller.signal);
      if (!mounted.current || controller.signal.aborted) return;
      setJob(undefined);
      onChange(next);
    } catch (value) {
      if (!mounted.current || controller.signal.aborted) return;
      setError(errorMessage(value));
    } finally {
      if (mounted.current && !controller.signal.aborted) setRetrying(false);
      if (retryController.current === controller) retryController.current = undefined;
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
