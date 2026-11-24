import type { ControlApi, OperationDetail, VisualFleetSnapshot } from "../api/types";
import { nodeDisplayName } from "../lib/fleet";
import {
  type ActivityStatus,
  type ActivitySummary,
  activityStatus,
  statusLabel,
  statusTone,
} from "./activity-status";

export type ActivityRecord = ActivitySummary & {
  occurred_at?: string | null | undefined;
  source: "job" | "operation";
  target_names?: string[];
  operation?: OperationDetail;
  /** The Controller's plain phrase for the event, and the workload it acts on. */
  summary?: string;
  subject?: string | null | undefined;
};
export type ActivityApi = Pick<
  ControlApi,
  "job" | "resumeJob" | "visualFleet" | "operations" | "operation"
>;

export function failureSummary(failure: OperationDetail["failure"]): string {
  if (!failure) return "";
  if ("code" in failure) return failure.detail;
  if ("reason" in failure) return failure.summary ?? failure.reason ?? failure.error_code ?? "";
  return failure.summary ?? failure.error_code ?? "";
}

export function failureText(failure: OperationDetail["failure"]): string {
  if (!failure) return "";
  return [
    failureSummary(failure),
    "detail" in failure ? failure.detail : "",
    "code" in failure ? failure.code : failure.error_code,
  ]
    .filter(Boolean)
    .join(" ");
}

const CATEGORY_LABELS: Record<string, string> = {
  operation: "Operations",
};

export function titleCase(value: string): string {
  return value.replace(/[_-]+/g, " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

export function activityCategory(event: ActivitySummary): string {
  const category = event.action.split(".")[0] || "other";
  return CATEGORY_LABELS[category] ?? titleCase(category);
}

function unavailableTargetLabel(target: string): string {
  return target.startsWith("spk_")
    ? "Spark no longer registered"
    : "Target not in current inventory";
}

export function targetSummaryNames(event: ActivityRecord): string[] {
  const names: string[] = [];
  let historicalSparks = 0;
  let historicalTargets = 0;
  event.targets.forEach((target, index) => {
    const name = event.target_names?.[index];
    if (name) names.push(name);
    else if (target.startsWith("spk_")) historicalSparks += 1;
    else historicalTargets += 1;
  });
  if (historicalSparks > 0)
    names.push(`${historicalSparks} historical ${historicalSparks === 1 ? "Spark" : "Sparks"}`);
  if (historicalTargets > 0)
    names.push(`${historicalTargets} historical ${historicalTargets === 1 ? "target" : "targets"}`);
  return names;
}

/** The entry's headline: the workload (else the Sparks) it concerns, then what happened, in the Controller's words. */
export function activityTitle(event: ActivityRecord): string {
  const names = targetSummaryNames(event);
  const sparks =
    names.length > 2 ? `${names.slice(0, 2).join(", ")} +${names.length - 2}` : names.join(", ");
  const subject = event.subject || sparks;
  const summary = event.summary || "Recorded activity";
  return subject ? `${subject} · ${summary}` : summary;
}

export function friendlyTarget(target: string, names: Map<string, string>): string {
  return names.get(target) || unavailableTargetLabel(target);
}

export function targetNameLookup(fleet: VisualFleetSnapshot | null): Map<string, string> {
  const names = new Map<string, string>();
  for (const node of fleet?.nodes ?? []) {
    names.set(node.id, nodeDisplayName(node));
  }
  return names;
}

const STANDALONE_JOB_PREFIX = "job:";

export function canonicalRecord(
  detail: OperationDetail,
  names: Map<string, string>,
): ActivityRecord {
  // Standalone jobs (agent upgrades) are listed as operations; their
  // progress and resume live in the job detail.
  if (detail.id.startsWith(STANDALONE_JOB_PREFIX)) {
    return {
      request_id: detail.id.slice(STANDALONE_JOB_PREFIX.length),
      actor: "Vonk Forge",
      action: `operation.${detail.kind}.${detail.state}`,
      occurred_at: detail.created_at,
      targets: detail.node_ids,
      target_names: detail.node_ids.map((id) => names.get(id) ?? ""),
      summary: detail.summary,
      subject: detail.subject,
      source: "job",
    };
  }
  return {
    request_id: detail.id,
    actor: "Vonk Forge",
    action: `operation.${detail.kind}.${detail.state}`,
    occurred_at: detail.created_at,
    targets: detail.node_ids,
    target_names: detail.node_ids.map((id) => names.get(id) ?? ""),
    summary: detail.summary,
    subject: detail.subject,
    source: "operation",
    operation: detail,
  };
}

function eventTimestamp(event: ActivityRecord): number {
  if (!event.occurred_at) return 0;
  const timestamp = Date.parse(event.occurred_at);
  return Number.isFinite(timestamp) ? timestamp : 0;
}

const STATUS_ORDER: Record<ActivityStatus, number> = {
  attention: 0,
  unsuccessful: 1,
  in_progress: 2,
  called_off: 2,
  unknown: 2,
  recorded: 2,
};

export function sortActivityByAttention(left: ActivityRecord, right: ActivityRecord): number {
  const statusDifference = STATUS_ORDER[activityStatus(left)] - STATUS_ORDER[activityStatus(right)];
  return statusDifference || eventTimestamp(right) - eventTimestamp(left);
}

export function sortActivityByTime(left: ActivityRecord, right: ActivityRecord): number {
  return eventTimestamp(right) - eventTimestamp(left);
}

/** The newest activity as short rows (what it was, to whom, how it ended), in the Activity page's own words. */
export function activityPreviewRows(
  operations: OperationDetail[],
  names: Map<string, string>,
  limit: number,
): {
  id: string;
  occurredAt: string | null;
  label: string;
  tone: ReturnType<typeof statusTone>;
  word: string;
}[] {
  return operations
    .map((operation) => canonicalRecord(operation, names))
    .sort(sortActivityByTime)
    .slice(0, limit)
    .map((record) => {
      const status = activityStatus(record);
      return {
        id: record.request_id,
        occurredAt: record.occurred_at ?? null,
        label: activityTitle(record),
        tone: statusTone(status),
        word: statusLabel(status),
      };
    });
}

/** How many of these operations ended unsuccessfully, by the same status the Activity filter uses. */
export function unsuccessfulCount(
  operations: OperationDetail[],
  names: Map<string, string>,
): number {
  return operations.filter(
    (operation) => activityStatus(canonicalRecord(operation, names)) === "unsuccessful",
  ).length;
}
