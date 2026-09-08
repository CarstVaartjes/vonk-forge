import {useEffect, useRef, useState} from "react";
import type {ControlApi, RunSwitchOperation, RunSwitchPhaseKind} from "../api/types";
import {formatBytes} from "../lib/fleet";
import {availabilityProgress, LibraryAvailabilityProgress} from "./library-availability-progress";

const TERMINAL_STATES = new Set(["succeeded", "failed", "cancelled", "partially_succeeded", "blocked"]);
export type RunSwitchApi = Pick<ControlApi, "getRecipeRunSwitchOperation" | "retryRecipeRunSwitch" | "cancelRecipeRunSwitchOperation">;

function errorMessage(value: unknown): string {
  return (value instanceof Error ? value.message : "The Controller did not return run progress.").slice(0, 256);
}

function phaseLabel(phase: RunSwitchPhaseKind | null, nodeName?: string, subphase?: RunSwitchOperation["progress"]["subphase"]): string {
  if (subphase === "container-build") return "Building container";
  if (subphase === "runtime-image") return "Preparing container image";
  if (subphase === "runtime-plan") return "Preparing installation";
  if (subphase === "runtime-install") return "Installing runtime";
  if (phase === "prepare") return "Preparing";
  if (subphase === "model-download") return nodeName ? `Copying model to ${nodeName}` : "Downloading model";
  if (phase === "transfer") return nodeName ? `Copying model to ${nodeName}` : "Downloading model";
  if (phase === "verify") return "Verifying copied artifacts";
  if (phase === "cleanup") return "Removing unreferenced copies";
  if (phase === "stop") return "Stopping the previous model";
  if (phase === "start") return "Starting";
  if (phase === "final_verify") return "Checking readiness";
  return "Starting";
}

function memberLabel(state: RunSwitchOperation["progress"]["members"][number]["state"]): string {
  if (state === "succeeded") return "Complete";
  if (state === "failed") return "Failed";
  if (state === "running") return "In progress";
  if (state === "unknown") return "Status unavailable";
  return "Waiting";
}

function byteDetail(completed: number, total: number | null | undefined, known: boolean): string {
  if (known && typeof total === "number") return `${formatBytes(completed)} of ${formatBytes(total)}`;
  return completed > 0 ? `${formatBytes(completed)} transferred · total unavailable` : "Total bytes unavailable";
}

export function LibraryRunSwitchProgress({api, nodeNames, onChange, onRefresh, operation, title}: {
  api: RunSwitchApi;
  nodeNames: Record<string, string>;
  onChange(operation: RunSwitchOperation): void;
  onRefresh?(): void;
  operation: RunSwitchOperation;
  title: string;
}) {
  const [pollError, setPollError] = useState("");
  const [retryError, setRetryError] = useState("");
  const [retrying, setRetrying] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState("");
  const cancelRequest = useRef<{id: string; key: string} | null>(null);
  const [pollAttempt, setPollAttempt] = useState(0);
  const progress = operation.progress;
  const activeMember = progress.members.find(member => member.state === "running") ?? progress.members.find(member => member.state === "pending");
  const activeName = activeMember ? nodeNames[activeMember.node_id] ?? activeMember.node_id : undefined;
  const failed = operation.state === "failed" || operation.state === "partially_succeeded" || operation.state === "blocked";

  useEffect(() => {
    if (TERMINAL_STATES.has(operation.state)) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void api.getRecipeRunSwitchOperation(operation.operation_id, controller.signal)
        .then(next => {
          if (controller.signal.aborted) return;
          setPollError("");
          onChange(next);
        })
        .catch(value => {
          if (!controller.signal.aborted) setPollError(errorMessage(value));
        });
    }, 900);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [api, onChange, operation, pollAttempt]);

  useEffect(() => {
    if (!TERMINAL_STATES.has(operation.state)) return;
    onRefresh?.();
  }, [onRefresh, operation.state]);

  async function retry() {
    setRetrying(true);
    setRetryError("");
    try {
      const next = await api.retryRecipeRunSwitch(operation.operation_id, {schema_version: 2, request_key: crypto.randomUUID()});
      setPollError("");
      onChange(next);
    } catch (value) {
      setRetryError(errorMessage(value));
    } finally {
      setRetrying(false);
    }
  }

  const canRetry = failed && operation.state === "failed" && operation.result?.retryable === true;
  const cancellation = operation.result?.cancellation;
  const starting = operation.completed_phases.includes("start") || (operation.state === "running" && ["start", "final_verify"].includes(progress.phase ?? ""));
  const canCancel = ["queued", "running"].includes(operation.state) && !starting && !cancellation;

  async function cancel() {
    if (cancelRequest.current?.id !== operation.operation_id) {
      cancelRequest.current = {id: operation.operation_id, key: crypto.randomUUID()};
    }
    setCancelling(true);
    setCancelError("");
    try {
      const next = await api.cancelRecipeRunSwitchOperation(operation.operation_id, {
        schema_version: 2, request_key: cancelRequest.current.key, reason: "Cancelled by operator",
      });
      onChange(next);
    } catch (value) {
      setCancelError(errorMessage(value));
    } finally {
      setCancelling(false);
    }
  }

  return <section className={`library-run-switch-progress state-${operation.state}`} aria-label={`${title} progress`} aria-live="polite">
    <header>
      <div><span>{operation.action === "switch" ? "Switch profile" : "Run"}</span><strong>{failed ? "Needs attention" : operation.state === "cancelled" ? "Cancelled" : operation.state === "succeeded" ? "Running" : phaseLabel(progress.phase, activeName, progress.subphase)}</strong></div>
      <small>{operation.state.replaceAll("_", " ")}</small>
    </header>
    <p className="library-run-switch-phase">{phaseLabel(progress.phase, activeName, progress.subphase)}{progress.phase_index + 1 <= progress.phase_count ? ` · phase ${progress.phase_index + 1} of ${progress.phase_count}` : ""}</p>
    <LibraryAvailabilityProgress progress={availabilityProgress(progress)}/>
    <ul className="library-run-switch-members" aria-label="Spark progress">
      {progress.members.map(member => {
        const name = nodeNames[member.node_id] ?? member.node_id;
        const memberKnown = typeof member.total_bytes === "number";
        return <li key={member.node_id} className={`state-${member.state}`}>
          <div><strong>{name}</strong><span>{memberLabel(member.state)}</span></div>
          <small>{byteDetail(member.completed_bytes, member.total_bytes, memberKnown)}</small>
          {member.error && <p className="is-error">{member.error}</p>}
        </li>;
      })}
    </ul>
    {operation.status_reason && <p className={failed ? "is-error" : "library-run-switch-reason"} role={failed ? "alert" : "status"}>{operation.status_reason}</p>}
    {pollError && <div className="library-run-switch-error" role="alert"><span>{pollError}</span><button type="button" className="button secondary" onClick={() => { setPollError(""); setPollAttempt(value => value + 1); }}>Retry progress</button></div>}
    {retryError && <p className="library-run-switch-error" role="alert">{retryError}</p>}
    {cancelError && <p className="library-run-switch-error" role="alert">{cancelError}</p>}
    {canCancel && <button type="button" className="button secondary" disabled={cancelling} onClick={() => void cancel()}>{cancelling ? "Requesting cancellation…" : "Cancel preparation"}</button>}
    {cancellation && operation.state !== "cancelled" && <p role="status">Cancellation requested. The current shared transfer or build may finish; cached files are retained.</p>}
    {starting && !TERMINAL_STATES.has(operation.state) && <p>Runtime startup has begun. Use Stop to end the run.</p>}
    {canRetry && <div className="library-run-switch-recovery"><span>Completed Spark copies are retained. Retry reconciles the remaining phases.</span><button type="button" className="button secondary" disabled={retrying} onClick={() => void retry()}>{retrying ? "Retrying run…" : "Retry run"}</button></div>}
    <details className="library-run-switch-advanced"><summary>Operation details</summary><dl><div><dt>Operation</dt><dd><code>{operation.operation_id}</code></dd></div><div><dt>Plan digest</dt><dd><code>{operation.plan_digest}</code></dd></div><div><dt>Request key</dt><dd><code>{operation.request_key}</code></dd></div></dl></details>
  </section>;
}
