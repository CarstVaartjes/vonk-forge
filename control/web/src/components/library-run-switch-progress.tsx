import {useEffect, useState} from "react";
import type {ControlApi, RunSwitchOperation, RunSwitchPhaseKind} from "../api/types";
import {formatBytes} from "../lib/fleet";

const TERMINAL_STATES = new Set(["succeeded", "failed", "cancelled", "partially_succeeded", "blocked"]);
export type RunSwitchApi = Pick<ControlApi, "getRecipeRunSwitchOperation">;

function errorMessage(value: unknown): string {
  return (value instanceof Error ? value.message : "The Controller did not return run progress.").slice(0, 256);
}

function phaseLabel(phase: RunSwitchPhaseKind | null, nodeName?: string, subphase?: RunSwitchOperation["progress"]["subphase"]): string {
  if (subphase === "container-build" || phase === "prepare") return "Building container";
  if (subphase === "model-download") return nodeName ? `Copying model to ${nodeName}` : "Downloading model";
  if (subphase === "runtime-install") return nodeName ? `Copying container to ${nodeName}` : "Copying container to NAS";
  if (phase === "transfer") return nodeName ? `Copying model to ${nodeName}` : "Downloading model";
  if (phase === "verify") return nodeName ? `Copying model to ${nodeName}` : "Downloading model";
  if (phase === "cleanup") return "Removing unreferenced copies";
  if (phase === "stop") return "Stopping the previous model";
  if (phase === "start") return "Starting";
  if (phase === "final_verify") return "Running";
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
  const totalBytes = progress.total_bytes;
  const totalKnown = progress.total_bytes_known && typeof totalBytes === "number";
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
      <div><span>{operation.action === "switch" ? "Switch profile" : "Run"}</span><strong>{failed ? "Needs attention" : operation.state === "succeeded" ? "Running" : phaseLabel(progress.phase, activeName, progress.subphase)}</strong></div>
      <small>{operation.state.replaceAll("_", " ")}</small>
    </header>
    <p className="library-run-switch-phase">{phaseLabel(progress.phase, activeName, progress.subphase)}{progress.phase_index + 1 <= progress.phase_count ? ` · phase ${progress.phase_index + 1} of ${progress.phase_count}` : ""}</p>
    {totalKnown
      ? <div className="library-run-switch-track" role="progressbar" aria-label="Run bytes transferred" aria-valuemin={0} aria-valuemax={totalBytes} aria-valuenow={progress.completed_bytes}><span style={{width: `${totalBytes === 0 ? 100 : Math.min(100, progress.completed_bytes / totalBytes * 100)}%`}}/></div>
      : <div className="library-run-switch-track is-indeterminate" role="progressbar" aria-label="Run progress" aria-valuetext="Total bytes unavailable"><span/></div>}
    <p className="library-run-switch-bytes">{byteDetail(progress.completed_bytes, progress.total_bytes, totalKnown)}</p>
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
    {pollError && <div className="library-run-switch-error" role="alert"><span>{pollError}</span><button type="button" className="button secondary" onClick={() => { setPollError(""); onRetry(); }}>Retry progress</button></div>}
    {failed && <div className="library-run-switch-recovery"><span>Completed Spark copies are retained. Retry reconciles the remaining phases.</span><button type="button" className="button secondary" onClick={onRetry}>Retry run</button></div>}
    <details className="library-run-switch-advanced"><summary>Operation details</summary><dl><div><dt>Operation</dt><dd><code>{operation.operation_id}</code></dd></div><div><dt>Plan digest</dt><dd><code>{operation.plan_digest}</code></dd></div><div><dt>Request key</dt><dd><code>{operation.request_key}</code></dd></div></dl></details>
  </section>;
}
