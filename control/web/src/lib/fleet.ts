import type {VisualFleetNode, VisualFleetSnapshot} from "../api/types";

export type TelemetryFreshness = "live" | "delayed" | "stale";
export type NodeOperationalState = TelemetryFreshness | "offline";

const LIVE_MAXIMUM_MS = 6_000;
const DELAYED_MAXIMUM_MS = 20_000;

export function telemetryFreshnessAt(observedAt: string | null | undefined, now: Date): TelemetryFreshness {
  if (!observedAt) return "stale";
  const observed = Date.parse(observedAt);
  const current = now.getTime();
  if (!Number.isFinite(observed) || !Number.isFinite(current)) return "stale";
  const age = Math.max(0, current - observed);
  if (age <= LIVE_MAXIMUM_MS) return "live";
  if (age <= DELAYED_MAXIMUM_MS) return "delayed";
  return "stale";
}

export function nodeOperationalState(node: VisualFleetNode, now: Date): NodeOperationalState {
  if (node.connection.online_state !== "online") return "offline";
  return telemetryFreshnessAt(node.telemetry?.sample.observed_at, now);
}

const OFFLINE_REASON_LABELS: Record<NonNullable<VisualFleetNode["connection"]["offline_reason"]>, string> = {
  "unregistered": "Node is not registered",
  "agent-inactive": "Agent is inactive",
  "agent-revoked": "Agent was revoked",
  "never-seen": "Agent has never connected",
  "last-seen-in-future": "Agent clock is ahead",
  "stale": "Agent presence timed out",
  "certificate-missing": "Certificate missing",
  "certificate-not-yet-valid": "Certificate not yet valid",
  "certificate-expired": "Certificate expired",
  "certificate-revoked": "Certificate revoked",
  "certificate-inactive": "Certificate inactive",
};

export function offlineReasonLabel(reason: VisualFleetNode["connection"]["offline_reason"]): string {
  return reason ? OFFLINE_REASON_LABELS[reason] : "Offline reason unavailable";
}

export function formatMetric(value: number | null | undefined, format: (value: number) => string): string {
  return typeof value === "number" && Number.isFinite(value) ? format(value) : "Not reported";
}

export function formatBytes(value: number | null | undefined): string {
  return formatMetric(value, bytes => {
    if (bytes < 1024) return `${Math.round(bytes)} B`;
    if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KiB`;
    if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
    return `${(bytes / 1024 ** 3).toFixed(1)} GiB`;
  });
}

function groupReason(reason: string | null | undefined): string {
  return reason ? reason.replaceAll("-", " ") : "reason unavailable";
}

export function installationGroupLabel(group: VisualFleetNode["installed"][number]): string {
  const ranks = `${group.present_ranks.length} of ${group.expected_rank_count} ranks`;
  return group.complete
    ? `Complete · ${ranks}`
    : `Partial · ${ranks} · ${groupReason(group.degraded_reason)}`;
}

export function runGroupLabel(group: VisualFleetNode["loaded"][number]): string {
  const ranks = `${group.present_ranks.length} of ${group.expected_rank_count} ranks`;
  return group.healthy
    ? `Healthy · ${ranks}`
    : `Degraded · ${ranks} · ${groupReason(group.degraded_reason)}`;
}

export type FleetSummary = {
  delayed: number;
  live: number;
  loadedRecipes: number;
  offline: number;
  stale: number;
  total: number;
  unifiedAvailableBytes: number;
  warnings: number;
};

export function summarizeFleet(snapshot: VisualFleetSnapshot, now: Date): FleetSummary {
  const summary: FleetSummary = {
    delayed: 0,
    live: 0,
    loadedRecipes: 0,
    offline: 0,
    stale: 0,
    total: snapshot.nodes.length,
    unifiedAvailableBytes: 0,
    warnings: 0,
  };
  const loaded = new Set<string>();
  for (const node of snapshot.nodes) {
    const state = nodeOperationalState(node, now);
    summary[state] += 1;
    if (state === "stale" || state === "offline") summary.warnings += 1;
    for (const run of node.loaded) loaded.add(run.run_id);
    if (state !== "live") continue;
    const hostFree = node.telemetry?.sample.memory_available_bytes;
    const gpuFree = node.telemetry?.sample.gpu_memory_free_bytes;
    if (typeof hostFree === "number" && Number.isFinite(hostFree)
        && typeof gpuFree === "number" && Number.isFinite(gpuFree)) {
      summary.unifiedAvailableBytes += Math.min(hostFree, gpuFree);
    }
  }
  summary.loadedRecipes = loaded.size;
  return summary;
}
