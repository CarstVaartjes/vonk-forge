import {useEffect, useState} from "react";
import type {ControlApi, RunSwitchOperation, RunSwitchPhaseKind} from "../api/types";
import {formatBytes} from "../lib/fleet";

const TERMINAL_STATES = new Set(["succeeded", "failed", "cancelled", "partially_succeeded", "blocked"]);
export type RunSwitchApi = Pick<ControlApi, "getRecipeRunSwitchOperation">;

function errorMessage(value: unknown): string {
  return (value instanceof Error ? value.message : "The Controller did not return run progress.").slice(0, 256);
}

function phaseLabel(phase: RunSwitchPhaseKind | null, nodeName?: string): string {
  if (phase === "transfer") return nodeName ? `Copying model and container to ${nodeName}` : "Downloading model and container";
  if (phase === "verify") return nodeName ? `Verifying model and container on ${nodeName}` : "Verifying model and container";
  if (phase === "prepare") return "Building container";
  if (phase === "cleanup") return "Cleaning up retained copies";
  if (phase === "stop") return "Stopping the previous model";
  if (phase === "start") return "Starting";
  if (phase === "final_verify") return "Checking model";
  return "Starting";
}

function memberLabel(state: RunSwitchOperation["progress"]["members"][number]["state"]): string {
  if (state === "succeeded") return "Complete";
  if (state === "failed") return "Failed";
  if (state === "running") return "In progress";
  if (state === "unknown") return "Status unavailable";
  return "Waiting";
}

function byteDetail(completed: number, total: number | null, known: boolean): string {
  if (known && total !== null) return `${formatBytes(completed)} of ${formatBytes(total)}`;
  return completed > 0 ? `${formatBytes(completed)} transferred · total unavailable` : "Total bytes unavailable";
}

export function LibraryRunSwitchProgress({api, nodeNames, onChange, onRefresh, onRetry, operation, title}: {
  api: RunSwitchApi;
  nodeNames: Record<string, string>;
  onChange(operation: RunSwitchOperation): void;
  onRefresh?(): void;
  onRetry(): void;
  operation: RunSwitchOperation;
  title: string;
}) {
  const [pollError, setPollError] = useState("");
  const progress = operation.progress;
  const totalKnown = progress.total_bytes_known && progress.total_bytes !== null;
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
  }, [api, onChange, operation.operation_id, operation.state]);

  useEffect(() => {
    if (!TERMINAL_STATES.has(operation.state)) return;
    onRefresh?.();
  }, [onRefresh, operation.state]);

  return <section className={`library-run-switch-progress state-${operation.state}`} aria-label={`${title} progress`} aria-live="polite">
    <header>
      <div><span>{operation.action === "switch" ? "Switch profile" : "Run"}</span><strong>{failed ? "Needs attention" : operation.state === "succeeded" ? "Running" : phaseLabel(progress.phase, activeName)}</strong></div>
      <small>{operation.state.replaceAll("_", " ")}</small>
    </header>
    <p className="library-run-switch-phase">{phaseLabel(progress.phase, activeName)}{progress.phase_index + 1 <= progress.phase_count ? ` · phase ${progress.phase_index + 1} of ${progress.phase_count}` : ""}</p>
    {totalKnown && progress.total_bytes !== null
      ? <div className="library-run-switch-track" role="progressbar" aria-label="Run bytes transferred" aria-valuemin={0} aria-valuemax={progress.total_bytes} aria-valuenow={progress.completed_bytes}><span style={{width: `${progress.total_bytes === 0 ? 100 : Math.min(100, progress.completed_bytes / progress.total_bytes * 100)}%`}}/></div>
      : <div className="library-run-switch-track is-indeterminate" role="progressbar" aria-label="Run progress" aria-valuetext="Total bytes unavailable"><span/></div>}
    <p className="library-run-switch-bytes">{byteDetail(progress.completed_bytes, progress.total_bytes, totalKnown)}</p>
    <ul className="library-run-switch-members" aria-label="Spark progress">
      {progress.members.map(member => {
        const name = nodeNames[member.node_id] ?? member.node_id;
        const memberKnown = member.total_bytes !== null;
        return <li key={member.node_id} className={`state-${member.state}`}>
          <div><strong>{name}</strong><span>{memberLabel(member.state)}</span></div>
          <small>{byteDetail(member.completed_bytes, member.total_bytes, memberKnown)}</small>
          {member.error && <p className="is-error">{member.error}</p>}
        </li>;
      })}
    </ul>
    {operation.status_reason && <p className={failed ? "is-error" : "library-run-switch-reason"} role={failed ? "alert" : "status"}>{operation.status_reason}</p>}
    {pollError && <div className="library-run-switch-error" role="alert"><span>{pollError}</span><button type="button" className="button secondary" onClick={() => { setPollError(""); onRetry(); }}>Retry progress</button></div>}
    {failed && <div className="library-run-switch-recovery"><span>Completed Spark copies are retained. Retry reconciles the remaining phases.</span><button type="button" className="button secondary" onClick={onRetry}>Retry run</button></div>}
    <details className="library-run-switch-advanced"><summary>Operation details</summary><dl><div><dt>Operation</dt><dd><code>{operation.operation_id}</code></dd></div><div><dt>Plan digest</dt><dd><code>{operation.plan_digest}</code></dd></div><div><dt>Request key</dt><dd><code>{operation.request_key}</code></dd></div></dl></details>
  </section>;
}
