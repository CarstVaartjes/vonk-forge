import type {VisualFleetNode, VisualFleetSnapshot} from "../api/types";

export type TelemetryFreshness = "live" | "delayed" | "stale";
export type NodeOperationalState = TelemetryFreshness | "offline";

export type NodeMemoryCapacity = {
  available: number;
  total: number;
  used: number;
  utilizationPercent: number;
};

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

const TELEMETRY_WARNING_CODES = new Set<VisualFleetNode["warnings"][number]["code"]>([
  "telemetry.missing",
  "telemetry.delayed",
  "telemetry.stale",
]);

export function reconcileTelemetryWarnings(
  warnings: VisualFleetNode["warnings"],
  freshness: TelemetryFreshness,
): VisualFleetNode["warnings"] {
  const insertionIndex = warnings.findIndex(warning => TELEMETRY_WARNING_CODES.has(warning.code));
  const reconciled = warnings.filter(warning => !TELEMETRY_WARNING_CODES.has(warning.code));
  if (freshness === "live") return reconciled;
  const warning: VisualFleetNode["warnings"][number] = freshness === "delayed"
    ? {code: "telemetry.delayed", detail: "Telemetry delivery is delayed.", severity: "warning"}
    : {code: "telemetry.stale", detail: "Telemetry is stale.", severity: "warning"};
  reconciled.splice(insertionIndex < 0 ? reconciled.length : Math.min(insertionIndex, reconciled.length), 0, warning);
  return reconciled;
}

export function nodeWarningsAt(node: VisualFleetNode, now: Date): VisualFleetNode["warnings"] {
  if (!node.telemetry?.sample) return node.warnings;
  return reconcileTelemetryWarnings(node.warnings, telemetryFreshnessAt(node.telemetry.sample.observed_at, now));
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

const SPARK_ID = /^spk_[0-9a-f]{32}$/i;
const SPARK_HOSTNAME = /^spk_[0-9a-f]{32}(?:\.|$)/i;

export function isTechnicalSparkIdentity(value: string | null | undefined): boolean {
  const normalized = value?.trim() ?? "";
  return SPARK_ID.test(normalized) || SPARK_HOSTNAME.test(normalized);
}

function humanizeName(value: string): string {
  return value
    .trim()
    .replace(/[._-]+/g, " ")
    .replace(/\s+/g, " ")
    .replace(/\b\p{L}/gu, character => character.toLocaleUpperCase());
}

export function nodeDisplayName(node: VisualFleetNode): string {
  const explicit = node.display_name.trim();
  if (explicit && !isTechnicalSparkIdentity(explicit)) return explicit;

  for (const key of ["display_name", "name", "spark_name"] as const) {
    const candidate = node.labels[key]?.trim();
    if (candidate && !isTechnicalSparkIdentity(candidate)) return humanizeName(candidate);
  }

  const hostname = node.hostname.trim();
  if (hostname && !isTechnicalSparkIdentity(hostname)) {
    const shortHostname = hostname.split(".")[0] ?? hostname;
    if (shortHostname) return humanizeName(shortHostname);
  }

  const role = node.labels.role?.trim();
  return role ? `${humanizeName(role)} Spark` : "Unnamed Spark";
}

export function nodeSecondaryName(node: VisualFleetNode): string | null {
  const hostname = node.hostname.trim();
  if (!hostname || isTechnicalSparkIdentity(hostname)) return null;
  const primary = nodeDisplayName(node);
  return hostname.localeCompare(primary, undefined, {sensitivity: "accent"}) === 0 ? null : hostname;
}

export function timestampPresentation(value: string | null | undefined, now: Date, prefix = "Updated"): {dateTime: string; exact: string; relative: string} | null {
  if (!value) return null;
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) return null;
  const deltaSeconds = Math.max(0, Math.round((now.getTime() - parsed.getTime()) / 1000));
  let amount = deltaSeconds;
  let unit = "second";
  if (deltaSeconds >= 86_400) {
    amount = Math.floor(deltaSeconds / 86_400);
    unit = "day";
  } else if (deltaSeconds >= 3_600) {
    amount = Math.floor(deltaSeconds / 3_600);
    unit = "hour";
  } else if (deltaSeconds >= 60) {
    amount = Math.floor(deltaSeconds / 60);
    unit = "minute";
  }
  return {
    dateTime: parsed.toISOString(),
    exact: parsed.toLocaleString([], {dateStyle: "medium", timeStyle: "long"}),
    relative: `${prefix} ${amount} ${unit}${amount === 1 ? "" : "s"} ago`,
  };
}

function finite(value: number | null | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

export function nodeUnifiedMemory(node: VisualFleetNode): NodeMemoryCapacity | null {
  const sample = node.telemetry?.sample;
  const hostAvailable = finite(sample?.memory_available_bytes ?? node.inventory?.host_memory_free_bytes);
  const hostTotal = finite(sample?.memory_total_bytes ?? node.inventory?.host_memory_total_bytes);
  const gpuAvailable = finite(sample?.gpu_memory_free_bytes ?? node.inventory?.gpu_memory_free_bytes);
  const gpuTotal = finite(sample?.gpu_memory_total_bytes ?? node.inventory?.gpu_memory_total_bytes);
  if (hostAvailable === null || hostTotal === null || gpuAvailable === null || gpuTotal === null) return null;
  const total = Math.min(hostTotal, gpuTotal);
  if (total <= 0) return null;
  const available = Math.min(total, Math.min(hostAvailable, gpuAvailable));
  const used = Math.max(0, total - available);
  return {available, total, used, utilizationPercent: (used / total) * 100};
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
  installedRecipes: number;
  live: number;
  loadedRecipes: number;
  offline: number;
  stale: number;
  total: number;
  unifiedAvailableBytes: number | null;
  unifiedCapacity: "known" | "partial" | "unknown";
  unifiedReportingNodes: number;
  unifiedTotalBytes: number | null;
  warnings: number;
};

function warningConditionKey(nodeId: string, code: VisualFleetNode["warnings"][number]["code"]): string {
  if (TELEMETRY_WARNING_CODES.has(code)) return `${nodeId}:telemetry`;
  return `${nodeId}:${code}`;
}

export function summarizeFleet(snapshot: VisualFleetSnapshot, now: Date): FleetSummary {
  const summary: FleetSummary = {
    delayed: 0,
    installedRecipes: 0,
    live: 0,
    loadedRecipes: 0,
    offline: 0,
    stale: 0,
    total: snapshot.nodes.length,
    unifiedAvailableBytes: 0,
    unifiedCapacity: "unknown",
    unifiedReportingNodes: 0,
    unifiedTotalBytes: 0,
    warnings: 0,
  };
  const installed = new Set<string>();
  const loaded = new Set<string>();
  const warningConditions = new Set<string>();
  for (const node of snapshot.nodes) {
    const state = nodeOperationalState(node, now);
    summary[state] += 1;
    for (const warning of nodeWarningsAt(node, now)) {
      warningConditions.add(warningConditionKey(node.id, warning.code));
    }
    if (state === "delayed" || state === "stale") warningConditions.add(`${node.id}:telemetry`);
    if (state === "offline") warningConditions.add(`${node.id}:node.offline`);
    for (const installation of node.installed) {
      if (installation.complete) installed.add(installation.installation_id);
    }
    for (const run of node.loaded) {
      if (run.healthy) loaded.add(run.run_id);
    }
    if (state !== "live") continue;
    const unified = nodeUnifiedMemory(node);
    if (unified) {
      summary.unifiedAvailableBytes! += unified.available;
      summary.unifiedTotalBytes! += unified.total;
      summary.unifiedReportingNodes += 1;
    }
  }
  summary.installedRecipes = installed.size;
  summary.loadedRecipes = loaded.size;
  summary.warnings = warningConditions.size;
  if (summary.unifiedReportingNodes === 0) {
    summary.unifiedAvailableBytes = null;
    summary.unifiedTotalBytes = null;
    summary.unifiedCapacity = "unknown";
  } else if (summary.unifiedReportingNodes < summary.live) {
    summary.unifiedCapacity = "partial";
  } else {
    summary.unifiedCapacity = "known";
  }
  return summary;
}
