import {
  addWire,
  compareWire,
  displayRatio,
  formatWire,
  isWireNumber,
  subtractWire,
  type WireNumber,
} from "../api/contract-numeric";
import type { SwapShortfallEvidence, VisualFleetNode, VisualFleetSnapshot } from "../api/types";
import {
  NodeOfflineReason,
  OutcomeKind,
  ProjectionCode,
  RunState,
} from "../api/vocabulary.generated";

export type TelemetryFreshness = "live" | "delayed" | "stale";
/** The words `vonkctl fleet` uses. */
export type NodeStatus = "online" | "needs attention" | "offline";

/** What an empty Fleet says, here and in `vonkctl fleet`. */
const NO_SPARKS = "No Sparks are enrolled.";

const LIVE_MAXIMUM_MS = 6_000;
const DELAYED_MAXIMUM_MS = 20_000;

export function telemetryFreshnessAt(
  observedAt: string | null | undefined,
  now: Date,
): TelemetryFreshness {
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
  ProjectionCode.TELEMETRY_MISSING,
  ProjectionCode.TELEMETRY_DELAYED,
  ProjectionCode.TELEMETRY_STALE,
]);

export function reconcileTelemetryWarnings(
  warnings: VisualFleetNode["warnings"],
  freshness: TelemetryFreshness,
): VisualFleetNode["warnings"] {
  const insertionIndex = warnings.findIndex((warning) => TELEMETRY_WARNING_CODES.has(warning.code));
  const reconciled = warnings.filter((warning) => !TELEMETRY_WARNING_CODES.has(warning.code));
  if (freshness === "live") return reconciled;
  const warning: VisualFleetNode["warnings"][number] =
    freshness === "delayed"
      ? {
          code: ProjectionCode.TELEMETRY_DELAYED,
          detail: "Telemetry delivery is delayed.",
          severity: "warning",
        }
      : {
          code: ProjectionCode.TELEMETRY_STALE,
          detail: "Telemetry is stale.",
          severity: "warning",
        };
  reconciled.splice(
    insertionIndex < 0 ? reconciled.length : Math.min(insertionIndex, reconciled.length),
    0,
    warning,
  );
  return reconciled;
}

/** A warning's sentence, followed by what to do about it when the Controller names one. */
export function warningText(warning: VisualFleetNode["warnings"][number]): string {
  return warning.recommendation ? `${warning.detail} ${warning.recommendation}` : warning.detail;
}

/** Online, offline, or online but needing attention, with the reasons in plain words. */
export function nodeStatus(
  node: VisualFleetNode,
  now: Date,
): { status: NodeStatus; reasons: string[] } {
  const projectionIssues = node.projection_issues ?? [];
  if (node.connection.online_state !== "online")
    return {
      status: "offline",
      reasons: [...projectionIssues, offlineReasonLabel(node.connection.offline_reason)],
    };
  const warnings = node.telemetry?.sample
    ? reconcileTelemetryWarnings(
        node.warnings,
        telemetryFreshnessAt(node.telemetry.sample.observed_at, now),
      )
    : node.warnings;
  // Informational notices (a newer recipe revision exists) are not attention.
  const reasons = [
    ...projectionIssues,
    ...warnings.filter((warning) => warning.severity !== "info").map(warningText),
  ];
  return { status: reasons.length > 0 ? "needs attention" : "online", reasons };
}

/** The status pill tone for a Spark's status word. */
export function nodeStatusTone(status: NodeStatus): "ok" | "warn" | "bad" {
  return status === "online" ? "ok" : status === "offline" ? "bad" : "warn";
}

/** "20 d 21 h", "6 h 42 m", "5 m": the two largest units; null when the Spark did not report it. */
export function uptimeText(seconds: number | null | undefined): string | null {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return null;
  const minutes = Math.floor(seconds / 60);
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor((minutes % 1440) / 60);
  if (days > 0) return `${days} d ${hours} h`;
  return hours > 0 ? `${hours} h ${minutes % 60} m` : `${minutes} m`;
}

/** One workload's state in a word. Health only qualifies a run that is running; any other run state is shown as is. */
export function runStateWord(run: VisualFleetNode["loaded"][number]): string {
  if (run.run_state == null) return "state unknown";
  if (run.run_state !== RunState.RUNNING) return run.run_state;
  if (run.healthy == null) return "state unknown";
  return run.healthy === false ? "degraded" : run.run_state;
}

/** The pill tone for a run, from the same facts as its word: failed or lost is bad, degraded warns, unknown is neutral. */
export function runStateTone(
  run: VisualFleetNode["loaded"][number],
): "ok" | "warn" | "bad" | "info" | "neutral" {
  if (run.run_state == null) return "neutral";
  if (run.run_state === RunState.FAILED || run.run_state === RunState.LOST) return "bad";
  if (run.run_state !== RunState.RUNNING) return "info";
  if (run.healthy == null) return "neutral";
  return run.healthy === false ? "warn" : "ok";
}

/**
 * What a Spark serves, one line per run: title, alias, state, and the other Sparks of a
 * multi-Spark run. `vonkctl fleet` prints the same lines.
 */
export function nodeWorkloadLines(node: VisualFleetNode, fleet: VisualFleetNode[]): string[] {
  const names = new Map(fleet.map((member) => [member.id, nodeDisplayName(member)]));
  const seen = new Set<string>();
  const lines: string[] = [];
  for (const run of node.loaded) {
    if (seen.has(run.run_id)) continue;
    seen.add(run.run_id);
    const partners = (run.member_node_ids ?? [])
      .filter((id) => id !== node.id)
      .map((id) => names.get(id) ?? id);
    lines.push(
      [
        run.title ?? run.run_id,
        run.alias,
        runStateWord(run),
        partners.length ? `with ${partners.join(", ")}` : null,
      ]
        .filter(Boolean)
        .join(" · "),
    );
  }
  return lines;
}

/** One workload and the Sparks that carry it, in rank order. */
export type WorkloadGroup = {
  runId: string;
  title: string;
  alias: string | null;
  /** The run's state in a word, as `nodeWorkloadLines` prints it. */
  state: string;
  tone: ReturnType<typeof runStateTone>;
  nodes: VisualFleetNode[];
  /** Each member Spark's rank in this run. */
  ranks: Record<string, number>;
};

/** Workloads that share a Spark stack as one cluster over one set of Spark cards. */
export type WorkloadCluster = { units: WorkloadGroup[]; nodes: VisualFleetNode[] };

/**
 * The fleet's workloads grouped into clusters (in order of first appearance), and the Sparks that run
 * none. A Spark carrying two workloads sits in one cluster with both, shown once.
 */
export function groupByWorkload(nodes: readonly VisualFleetNode[]): {
  clusters: WorkloadCluster[];
  idle: VisualFleetNode[];
} {
  const units = new Map<string, WorkloadGroup>();
  for (const node of nodes) {
    for (const run of node.loaded) {
      const unit: WorkloadGroup = units.get(run.run_id) ?? {
        runId: run.run_id,
        title: run.title ?? run.run_id,
        alias: run.alias ?? null,
        state: runStateWord(run),
        tone: runStateTone(run),
        nodes: [],
        ranks: {},
      };
      if (!(node.id in unit.ranks)) {
        unit.nodes.push(node);
        unit.ranks[node.id] = Number(run.rank ?? 0);
      }
      units.set(run.run_id, unit);
    }
  }
  const clusters: WorkloadCluster[] = [];
  for (const unit of units.values()) {
    unit.nodes.sort((a, b) => (unit.ranks[a.id] ?? 0) - (unit.ranks[b.id] ?? 0));
    const shared = clusters.filter((cluster) =>
      cluster.nodes.some((node) => unit.nodes.some((member) => member.id === node.id)),
    );
    const merged: WorkloadCluster = { units: [unit], nodes: [...unit.nodes] };
    for (const cluster of shared) {
      merged.units.unshift(...cluster.units);
      for (const node of cluster.nodes)
        if (!merged.nodes.some((member) => member.id === node.id)) merged.nodes.push(node);
      clusters.splice(clusters.indexOf(cluster), 1);
    }
    clusters.push(merged);
  }
  return { clusters, idle: nodes.filter((node) => node.loaded.length === 0) };
}

export type FleetSummary = {
  enrolled: number;
  online: number;
  /** Online Sparks that reported a free-memory sample; the sum covers only these. */
  reporting: number;
  freeMemoryBytes: WireNumber | null;
};

export function summarizeFleet(fleet: VisualFleetSnapshot | undefined): FleetSummary | undefined {
  if (!fleet) return undefined;
  const online = fleet.nodes.filter((node) => node.connection.online_state === "online");
  // The fleet's per-node memory rule: the node's latest telemetry sample, shared with `fleetMemory`.
  const samples = online.flatMap((node) => {
    const value = node.telemetry?.sample.memory_available_bytes;
    return value === null || value === undefined ? [] : [value as WireNumber];
  });
  return {
    enrolled: fleet.nodes.length,
    online: online.length,
    reporting: samples.length,
    freeMemoryBytes: samples.length
      ? samples.reduce<WireNumber>((total, value) => addWire(total, value), 0)
      : null,
  };
}

/**
 * GPU power now, summed over the online Sparks whose live sample reports it. A plain sum of Spark
 * readings kept here until the Controller summarises power (M2); then this moves there.
 */
export function fleetPower(
  nodes: readonly VisualFleetNode[],
): { watts: number; reporting: number } | null {
  const readings = nodes.flatMap((node) => {
    const watts =
      node.connection.online_state === "online" ? liveSample(node)?.gpu_power_watts : null;
    return watts == null ? [] : [Number(formatWire(watts))];
  });
  return readings.length
    ? { watts: readings.reduce((total, watts) => total + watts, 0), reporting: readings.length }
    : null;
}

/**
 * Unified memory summed over the online Sparks that report it, by the rule `summarizeFleet` uses (the
 * node's latest telemetry sample), so the status sentence and the band agree. Models and system are
 * summed only when every counted Spark reports the split; otherwise the sum is Used and Free.
 */
export function fleetMemory(
  nodes: readonly VisualFleetNode[],
): { split: MemorySplit; reporting: number } | null {
  const splits = nodes.flatMap((node) => {
    const sample = node.connection.online_state === "online" ? node.telemetry?.sample : null;
    const total = sample?.memory_total_bytes;
    const free = sample?.memory_available_bytes;
    if (!sample || !isWireNumber(total) || !isWireNumber(free) || compareWire(total, 0) <= 0)
      return [];
    const models = sample.gpu_process_memory_bytes;
    const system = sample.system_memory_bytes;
    const reported = isWireNumber(models) && isWireNumber(system);
    return [
      {
        totalBytes: total,
        usedBytes: subtractWire(total, free),
        freeBytes: free,
        modelsBytes: reported ? models : null,
        systemBytes: reported ? system : null,
      } satisfies MemorySplit,
    ];
  });
  if (!splits.length) return null;
  const sum = (pick: (split: MemorySplit) => WireNumber) =>
    splits.reduce<WireNumber>((total, split) => addWire(total, pick(split)), 0);
  const whole = splits.every((split) => split.modelsBytes !== null && split.systemBytes !== null);
  return {
    reporting: splits.length,
    split: {
      totalBytes: sum((split) => split.totalBytes),
      usedBytes: sum((split) => split.usedBytes),
      freeBytes: sum((split) => split.freeBytes),
      modelsBytes: whole ? sum((split) => split.modelsBytes as WireNumber) : null,
      systemBytes: whole ? sum((split) => split.systemBytes as WireNumber) : null,
    },
  };
}

/** Free memory across the fleet, saying so when only some online Sparks reported it; null when none did. */
export function freeMemoryText(summary: FleetSummary): string | null {
  if (summary.freeMemoryBytes === null) return null;
  const free = `${formatBytes(summary.freeMemoryBytes)} unified memory free`;
  return summary.reporting === summary.online
    ? free
    : `${free} on ${summary.reporting} of ${summary.online} online Sparks`;
}

/** A Spark needs attention when it is offline or warns, or its inventory is stale. */
export function nodeNeedsAttention(node: VisualFleetNode, now: Date): boolean {
  return nodeStatus(node, now).status !== "online" || node.inventory?.freshness === "stale";
}

/** A workload is serving while it is running and reported healthy. */
function isServing(run: VisualFleetNode["loaded"][number]): boolean {
  return run.run_state === RunState.RUNNING && run.healthy === true;
}

/**
 * The Fleet in one line: Sparks online, unified memory free, workloads serving, and what needs
 * attention (Sparks, then firing alerts). `vonkctl fleet` prints the same line.
 */
export function fleetStatusLine(fleet: VisualFleetSnapshot, now: Date): string {
  if (fleet.nodes.length === 0) return NO_SPARKS;
  const summary = summarizeFleet(fleet) as FleetSummary;
  const serving = new Set(
    fleet.nodes.flatMap((node) => node.loaded.filter(isServing).map((run) => run.run_id)),
  ).size;
  const sparks = fleet.nodes.filter((node) => nodeNeedsAttention(node, now)).length;
  const alerts = fleet.attention?.length ?? 0;
  const attention = [
    sparks > 0 ? `${sparks} ${sparks === 1 ? "Spark needs" : "Sparks need"} attention` : null,
    alerts > 0 ? `${alerts} ${alerts === 1 ? "alert" : "alerts"} firing` : null,
    alerts === 0 && fleet.attention_unavailable ? "alerts not reported" : null,
  ].filter((part): part is string => part !== null);
  return [
    `${summary.online} of ${summary.enrolled} ${summary.enrolled === 1 ? "Spark" : "Sparks"} online`,
    freeMemoryText(summary) ?? "unified memory not reported",
    serving === 0
      ? "no workloads serving"
      : `${serving} ${serving === 1 ? "workload" : "workloads"} serving`,
    ...(attention.length > 0 ? attention : ["nothing needs attention"]),
  ].join(" · ");
}

/**
 * A firing alert in plain words: its severity as a word, and its summary with the Spark it
 * names by display name rather than by identity. `vonkctl fleet` prints `severity: text`.
 */
export function attentionText(
  alert: NonNullable<VisualFleetSnapshot["attention"]>[number],
  fleet: VisualFleetNode[],
): { severity: string; text: string } {
  const spark = fleet.find((node) => node.id === alert.labels.node_id);
  const named = alert.labels.node_id
    ? ` (${spark ? nodeDisplayName(spark) : alert.labels.node_id})`
    : "";
  return {
    severity: alert.severity.charAt(0).toLocaleUpperCase() + alert.severity.slice(1),
    text: `${alert.summary}${named}`,
  };
}

/** Running workloads on an older recipe revision, once per run; informational and never an error. */
export function nodeRecipeUpdates(
  node: VisualFleetNode,
): { runId: string; title: string; detail: string }[] {
  const seen = new Set<string>();
  const updates: { runId: string; title: string; detail: string }[] = [];
  for (const run of node.loaded) {
    if (!run.recipe_update || seen.has(run.run_id)) continue;
    seen.add(run.run_id);
    updates.push({
      runId: run.run_id,
      title: run.title ?? run.run_id,
      detail: run.recipe_update.detail,
    });
  }
  return updates;
}

const OFFLINE_REASON_LABELS: Record<
  NonNullable<VisualFleetNode["connection"]["offline_reason"]>,
  string
> = {
  [NodeOfflineReason.UNREGISTERED]: "Node is not registered",
  [NodeOfflineReason.AGENT_INACTIVE]: "Agent is inactive",
  [NodeOfflineReason.AGENT_REVOKED]: "Agent was revoked",
  [NodeOfflineReason.NEVER_SEEN]: "Agent has never connected",
  [NodeOfflineReason.STALE]: "Agent presence timed out",
  [NodeOfflineReason.CERTIFICATE_MISSING]: "Certificate missing",
  [NodeOfflineReason.CERTIFICATE_NOT_YET_VALID]: "Certificate not yet valid",
  [NodeOfflineReason.CERTIFICATE_EXPIRED]: "Certificate expired",
  [NodeOfflineReason.CERTIFICATE_REVOKED]: "Certificate revoked",
  [NodeOfflineReason.CERTIFICATE_INACTIVE]: "Certificate inactive",
};

export function offlineReasonLabel(
  reason: VisualFleetNode["connection"]["offline_reason"],
): string {
  return reason ? OFFLINE_REASON_LABELS[reason] : "Offline reason unavailable";
}

/** .5 rounds up on every Fleet number; `vonkctl` does the same (Python's round() goes to even). */
export function halfUp(value: number): number {
  return Math.floor(value + 0.5);
}

export function formatMetric(
  value: number | null | undefined,
  format: (value: number) => string,
): string {
  return typeof value === "number" && Number.isFinite(value) ? format(value) : NOT_REPORTED_TEXT;
}

export function formatBytes(value: WireNumber | null | undefined): string {
  if (!isWireNumber(value)) return NOT_REPORTED_TEXT;
  if (compareWire(value, 1024) < 0) return `${formatWire(value)} B`;
  const unit =
    compareWire(value, 1024 ** 2) < 0
      ? ([1024, "KiB"] as const)
      : compareWire(value, 1024 ** 3) < 0
        ? ([1024 ** 2, "MiB"] as const)
        : compareWire(value, 1024 ** 4) < 0
          ? ([1024 ** 3, "GiB"] as const)
          : ([1024 ** 4, "TiB"] as const);
  const displayed = displayRatio(value, unit[0]);
  return Number.isFinite(displayed)
    ? `${(halfUp(displayed * 10) / 10).toFixed(1)} ${unit[1]}`
    : `${formatWire(value)} B`;
}

/** The Controller's swap numbers for one wait, shown as it computed them. */
export function swapShortfallText(evidence: SwapShortfallEvidence): string {
  const setting =
    evidence.desired_bytes == null
      ? "setting unknown"
      : `setting ${formatBytes(evidence.desired_bytes)}`;
  return (
    `Swap: ${formatBytes(evidence.active_bytes)} active (${setting}), ` +
    `${formatBytes(evidence.used_bytes)} in use, ` +
    `${formatBytes(evidence.held_bytes)} held by loading runs; ` +
    `usable ${formatBytes(evidence.usable_bytes)}; ` +
    `this load rises ${formatBytes(evidence.rise_bytes)}; ` +
    `needs a setting of at least ${formatBytes(evidence.needed_setting_bytes)}`
  );
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
    .replace(/\b\p{L}/gu, (character) => character.toLocaleUpperCase());
}

export function nodeDisplayName(node: VisualFleetNode): string {
  const explicit = node.display_name.trim();
  if (explicit && !isTechnicalSparkIdentity(explicit)) return explicit;

  for (const key of ["display_name", "name", "spark_name"] as const) {
    const candidate = node.labels?.[key]?.trim();
    if (candidate && !isTechnicalSparkIdentity(candidate)) return humanizeName(candidate);
  }

  const hostname = node.hostname.trim();
  if (hostname && !isTechnicalSparkIdentity(hostname)) {
    const shortHostname = hostname.split(".")[0] ?? hostname;
    if (shortHostname) return humanizeName(shortHostname);
  }

  const role = node.labels?.role?.trim();
  return role ? `${humanizeName(role)} Spark` : "Unnamed Spark";
}

export function nodeSecondaryName(node: VisualFleetNode): string | null {
  const hostname = node.hostname.trim();
  if (!hostname || isTechnicalSparkIdentity(hostname)) return null;
  const primary = nodeDisplayName(node);
  return hostname.localeCompare(primary, undefined, { sensitivity: "accent" }) === 0
    ? null
    : hostname;
}

/** The live telemetry sample while its freshness is live; otherwise the Spark shows its inventory. */
export function liveSample(node: VisualFleetNode) {
  return node.telemetry?.freshness === "live" ? node.telemetry.sample : null;
}

/** Used and total memory of the Spark, from the live sample when there is one. */
export function nodeMemory(node: VisualFleetNode): {
  usedPercent: number;
  usedBytes: WireNumber;
  freeBytes: WireNumber;
  totalBytes: WireNumber;
} | null {
  const sample = liveSample(node);
  const total = sample ? sample.memory_total_bytes : node.inventory?.host_memory_total_bytes;
  const free = sample ? sample.memory_available_bytes : node.inventory?.host_memory_free_bytes;
  if (!isWireNumber(total) || !isWireNumber(free) || compareWire(total, 0) <= 0) return null;
  const used = subtractWire(total, free);
  return {
    usedPercent: halfUp(100 * displayRatio(used, total)),
    usedBytes: used,
    freeBytes: free,
    totalBytes: total,
  };
}

export function nodeDiskFreeBytes(node: VisualFleetNode): WireNumber | null {
  return node.inventory?.disk_free_bytes ?? node.telemetry?.sample.disk_free_bytes ?? null;
}

const GIB = 1024 ** 3;
/** Free unified memory under this is a display warning (not an admission rule). */
const MEMORY_WARN_FREE_BYTES = 8 * GIB;
const MEMORY_SERIOUS_FREE_BYTES = 4 * GIB;

export type MemoryLevel = "ok" | "warn" | "serious";

/** The words beside the low-memory mark, built from the thresholds above. */
export const MEMORY_LOW_WORDS = {
  warn: `Under ${MEMORY_WARN_FREE_BYTES / GIB} GiB free`,
  serious: `Under ${MEMORY_SERIOUS_FREE_BYTES / GIB} GiB free`,
} as const;

/** Memory is judged by what is left, not by what is used: a model that owns the Spark is the normal state. */
export function memoryLevel(freeBytes: WireNumber): MemoryLevel {
  if (compareWire(freeBytes, MEMORY_SERIOUS_FREE_BYTES) < 0) return "serious";
  return compareWire(freeBytes, MEMORY_WARN_FREE_BYTES) < 0 ? "warn" : "ok";
}

/**
 * Where a Spark's unified memory goes. Models and system are what the Spark reported; when it did not
 * report both, they are null and the caller shows Used and Free only.
 */
export type MemorySplit = {
  totalBytes: WireNumber;
  freeBytes: WireNumber;
  usedBytes: WireNumber;
  modelsBytes: WireNumber | null;
  systemBytes: WireNumber | null;
};

export function nodeMemorySplit(node: VisualFleetNode): MemorySplit | null {
  const memory = nodeMemory(node);
  if (!memory) return null;
  const sample = liveSample(node);
  const models = sample?.gpu_process_memory_bytes;
  const system = sample?.system_memory_bytes;
  const reported = isWireNumber(models) && isWireNumber(system);
  return {
    totalBytes: memory.totalBytes,
    usedBytes: memory.usedBytes,
    freeBytes: memory.freeBytes,
    modelsBytes: reported ? models : null,
    systemBytes: reported ? system : null,
  };
}

/** One Fleet vital: its fill (null draws no meter), the words beside it, and the amber warning. */
type Vital = { percent: number | null; text: string; amber: boolean };

/** What every reading says when the Spark did not report it. */
export const NOT_REPORTED_TEXT = "Not reported";
const NOT_REPORTED: Vital = { percent: null, text: NOT_REPORTED_TEXT, amber: false };

/** Free disk and its total: the inventory's pair, else the live sample's. */
function diskPair(node: VisualFleetNode): { free: WireNumber; total: WireNumber } | null {
  for (const source of [node.inventory, liveSample(node)]) {
    const free = source?.disk_free_bytes;
    const total = source?.disk_total_bytes;
    if (isWireNumber(free) && isWireNumber(total) && compareWire(total, 0) > 0) {
      return { free, total };
    }
  }
  return null;
}

/** The three vitals of a Fleet row; `vonkctl fleet` prints the same words (shared vectors). */
export function nodeVitals(node: VisualFleetNode): { memory: Vital; disk: Vital; gpu: Vital } {
  const memory = nodeMemory(node);
  const disk = diskPair(node);
  const sample = liveSample(node);
  const utilisation = sample?.gpu_utilization_percent;
  const temperature = sample?.gpu_temperature_c;
  const readings = [
    typeof utilisation === "number" && Number.isFinite(utilisation)
      ? `${halfUp(utilisation)}%`
      : null,
    typeof temperature === "number" ? `${temperature} °C` : null,
  ].filter((reading): reading is string => reading !== null);
  const reason = sample?.gpu_unavailable_reason;
  const usedDisk =
    disk && halfUp(100 * displayRatio(subtractWire(disk.total, disk.free), disk.total));
  return {
    memory: memory
      ? {
          percent: memory.usedPercent,
          text: `${formatBytes(memory.usedBytes)} of ${formatBytes(memory.totalBytes)} (${memory.usedPercent}%)`,
          amber: memoryLevel(memory.freeBytes) !== "ok",
        }
      : NOT_REPORTED,
    disk:
      disk && usedDisk !== null
        ? {
            percent: Math.min(100, Math.max(0, 100 - usedDisk)),
            text: `${formatBytes(disk.free)} free of ${formatBytes(disk.total)}`,
            amber: usedDisk >= 90,
          }
        : NOT_REPORTED,
    // GPU utilisation is never amber: a busy GPU is the point of the machine.
    gpu: readings.length
      ? {
          percent:
            typeof utilisation === "number" && Number.isFinite(utilisation)
              ? Math.min(100, Math.max(0, utilisation))
              : null,
          text: readings.join(", "),
          amber: false,
        }
      : { ...NOT_REPORTED, text: reason ? `${NOT_REPORTED_TEXT} (${reason})` : NOT_REPORTED_TEXT },
  };
}

/** Average current CPU clock against the hardware maximum, with the temperature it runs at. */
export function nodeCpuClock(
  node: VisualFleetNode,
): { clock: string; temperature: string | null; lowClock: boolean } | null {
  const sample = node.telemetry?.sample;
  const current = sample?.cpu_frequency_avg_mhz;
  if (!sample || node.telemetry?.freshness === "stale" || typeof current !== "number") return null;
  const maximum = sample.cpu_frequency_max_mhz;
  const ghz = (mhz: number) => (mhz / 1000).toFixed(1);
  return {
    clock:
      typeof maximum === "number"
        ? `${ghz(current)} of ${ghz(maximum)} GHz`
        : `${ghz(current)} GHz`,
    temperature:
      typeof sample.gpu_temperature_c === "number" ? `${sample.gpu_temperature_c} °C` : null,
    lowClock: node.warnings.some((warning) => warning.code === ProjectionCode.CPU_LOW_CLOCK),
  };
}

/** Driver readings and explicitly labelled command receipt, never invented readback. */
export function nodeGpuClock(node: VisualFleetNode): string {
  const clock = node.telemetry?.sample?.gpu_clock;
  const range = (value: NonNullable<VisualFleetNode["gpu_clock_desired"]> | null | undefined) =>
    value ? `${value.min_mhz}–${value.max_mhz} MHz` : OutcomeKind.UNKNOWN;
  const when = (value: WireNumber) => {
    const date = new Date(Number(formatWire(value)) * 1000);
    return Number.isNaN(date.getTime())
      ? `${formatWire(value)} UTC epoch seconds`
      : date.toLocaleString();
  };
  return `Graphics ${clock?.graphics_mhz ?? OutcomeKind.UNKNOWN} / SM ${clock?.sm_mhz ?? OutcomeKind.UNKNOWN} MHz · last applied ${range(clock?.locked_range)} (helper receipt) · desired ${range(node.gpu_clock_desired)} · reported maximum graphics ${clock?.max_graphics_mhz ?? OutcomeKind.UNKNOWN} / SM ${clock?.max_sm_mhz ?? OutcomeKind.UNKNOWN} MHz · policy limit ${node.gpu_clock_policy?.hard_max_mhz ?? OutcomeKind.UNKNOWN} MHz · throttle bits ${clock?.throttle_reasons ?? OutcomeKind.UNKNOWN}${clock?.apply_state ? ` · enforcement ${clock.apply_state}` : ""}${clock?.next_attempt_at != null ? ` · next attempt ${when(clock.next_attempt_at)}` : ""}${clock?.deadline != null ? ` · deadline ${when(clock.deadline)}` : ""}${clock?.detail ? ` · ${clock.detail}` : ""}`;
}

/** Active swap beside the desired size and the helper's last receipt. */
export function nodeSwap(node: VisualFleetNode): string {
  const swap = node.inventory?.host_settings?.swap;
  const receipt = swap?.receipt;
  const desired = node.swap_desired ? `${node.swap_desired.size_gib} GiB` : OutcomeKind.UNKNOWN;
  const policy = node.swap_policy
    ? ` (policy ${node.swap_policy.minimum_gib}–${node.swap_policy.maximum_gib} GiB)`
    : "";
  const active = swap
    ? `${formatBytes(swap.total_bytes)} active · ${formatBytes(swap.free_bytes)} free`
    : OutcomeKind.UNKNOWN;
  const state = receipt
    ? ` · ${receipt.state}${receipt.reason ? ` (${receipt.reason})` : ""}${receipt.detail ? ` · ${receipt.detail}` : ""}`
    : swap?.receipt_detail
      ? ` · ${swap.receipt_detail}`
      : "";
  return `${active} · desired ${desired}${policy}${state}`;
}

/** Firmware capability and measured reservation are separate; neither proves Auto. */
export function nodeDisplayMemory(node: VisualFleetNode): string {
  const observation = node.inventory?.host_settings?.display_memory;
  const firmware = observation?.soc_firmware_version;
  const version =
    firmware == null ? OutcomeKind.UNKNOWN : `0x${BigInt(firmware).toString(16).padStart(8, "0")}`;
  const reserved = observation?.carveout_bytes;
  const connected = observation?.display_connected;
  return `Reserved ${reserved == null ? OutcomeKind.UNKNOWN : formatBytes(reserved)} · SoC ${version} · UEFI ${observation?.bios_version ?? OutcomeKind.UNKNOWN} · display connected ${connected == null ? OutcomeKind.UNKNOWN : String(connected)}`;
}
