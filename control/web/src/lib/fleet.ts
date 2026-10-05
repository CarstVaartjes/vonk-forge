import type {VisualFleetNode} from "../api/types";

export type TelemetryFreshness = "live" | "delayed" | "stale";
/** The words `vonkctl fleet` uses. */
export type NodeStatus = "online" | "needs attention" | "offline";

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

/** A warning's sentence, followed by what to do about it when the Controller names one. */
export function warningText(warning: VisualFleetNode["warnings"][number]): string {
  return warning.recommendation ? `${warning.detail} ${warning.recommendation}` : warning.detail;
}

/** Online, offline, or online but needing attention, with the reasons in plain words. */
export function nodeStatus(node: VisualFleetNode, now: Date): {status: NodeStatus; reasons: string[]} {
  if (node.connection.online_state !== "online") return {status: "offline", reasons: [offlineReasonLabel(node.connection.offline_reason)]};
  const warnings = node.telemetry?.sample
    ? reconcileTelemetryWarnings(node.warnings, telemetryFreshnessAt(node.telemetry.sample.observed_at, now))
    : node.warnings;
  // Informational notices (a newer recipe revision exists) are not attention.
  const reasons = warnings.filter(warning => warning.severity !== "info").map(warningText);
  return {status: reasons.length > 0 ? "needs attention" : "online", reasons};
}

/** Running workloads on an older recipe revision, once per run; informational and never an error. */
export function nodeRecipeUpdates(node: VisualFleetNode): {runId: string; title: string; detail: string}[] {
  const seen = new Set<string>();
  const updates: {runId: string; title: string; detail: string}[] = [];
  for (const run of node.loaded) {
    if (!run.recipe_update || seen.has(run.run_id)) continue;
    seen.add(run.run_id);
    updates.push({runId: run.run_id, title: run.title, detail: run.recipe_update.detail});
  }
  return updates;
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

/** Used share and total of the Spark's memory, from the live sample when there is one. */
export function nodeMemory(node: VisualFleetNode): {usedPercent: number; totalBytes: number} | null {
  const sample = node.telemetry?.sample;
  const total = sample?.memory_total_bytes ?? node.inventory?.host_memory_total_bytes;
  const free = sample?.memory_available_bytes ?? node.inventory?.host_memory_free_bytes;
  if (typeof total !== "number" || typeof free !== "number" || total <= 0) return null;
  return {usedPercent: Math.round(100 * (total - free) / total), totalBytes: total};
}

export function nodeDiskFreeBytes(node: VisualFleetNode): number | null {
  return node.inventory?.disk_free_bytes ?? node.telemetry?.sample.disk_free_bytes ?? null;
}

/** Average current CPU clock against the hardware maximum, with the temperature it runs at. */
export function nodeCpuClock(node: VisualFleetNode): {clock: string; temperature: string | null; lowClock: boolean} | null {
  const sample = node.telemetry?.sample;
  const current = sample?.cpu_frequency_avg_mhz;
  if (!sample || node.telemetry?.freshness === "stale" || typeof current !== "number") return null;
  const maximum = sample.cpu_frequency_max_mhz;
  const ghz = (mhz: number) => (mhz / 1000).toFixed(1);
  return {
    clock: typeof maximum === "number" ? `${ghz(current)} of ${ghz(maximum)} GHz` : `${ghz(current)} GHz`,
    temperature: typeof sample.gpu_temperature_c === "number" ? `${sample.gpu_temperature_c} °C` : null,
    lowClock: node.warnings.some(warning => warning.code === "cpu.low-clock"),
  };
}
