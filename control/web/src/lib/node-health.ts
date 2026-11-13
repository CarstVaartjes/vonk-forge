import type { VisualFleetNode, VisualFleetSnapshot } from "../api/types";
import { OutcomeKind } from "../api/vocabulary.generated";
import {
  MEMORY_LOW_WORDS,
  attentionText,
  attentionTone,
  formatBytes,
  liveSample,
  memoryLevel,
  nodeMemory,
  nodeRecipeUpdates,
  nodeStatus,
  nodeStatusTone,
} from "./fleet";
import { num } from "./gpu-readings";
import { GPU_TEMP_SERIOUS_C, GPU_TEMP_WARN_C } from "./gpu-thresholds";

type Tone = "ok" | "warn" | "bad" | "info" | "neutral";
const ORDER: Record<Tone, number> = { bad: 0, warn: 1, info: 2, neutral: 3, ok: 4 };

/**
 * What one Spark's Health card lists, worst first: its warnings and offline reasons, the firing alerts
 * that name it, a newer recipe revision, an incomplete installation (with its id, for the reconcile
 * action), and the all-clear findings the live snapshot can prove (GPU temperature, memory headroom, no
 * alerts). A reading that is not reported adds no row; nothing is filled in.
 */
export function nodeHealth(
  node: VisualFleetNode,
  nodes: readonly VisualFleetNode[],
  now: Date,
  attention: VisualFleetSnapshot["attention"],
  attentionUnavailable: boolean,
) {
  const rows: {
    key: string;
    tone: Tone;
    title: string;
    detail: string;
    installationId?: string;
  }[] = [];
  const { status, reasons } = nodeStatus(node, now);
  reasons.forEach((reason, index) =>
    rows.push({
      key: `status-${index}`,
      tone: nodeStatusTone(status),
      title: status === "offline" ? "Offline" : "Attention",
      detail: reason,
    }),
  );
  const alerts = (attention ?? []).filter((alert) => alert.labels.node_id === node.id);
  for (const alert of alerts)
    rows.push({
      key: `alert-${JSON.stringify(alert.labels)}`,
      tone: attentionTone(alert),
      title: attentionText(alert, [...nodes]).severity,
      detail: alert.summary,
    });
  for (const update of nodeRecipeUpdates(node))
    rows.push({
      key: `update-${update.runId}`,
      tone: "info",
      title: "Update available",
      detail: update.detail,
    });
  for (const item of node.installed.filter((entry) => entry.complete === false))
    rows.push({
      key: `install-${item.installation_id}`,
      tone: "warn",
      title: item.group_state ?? OutcomeKind.UNKNOWN,
      detail: `${item.title} · ${item.present_ranks?.length ?? OutcomeKind.UNKNOWN} of ${item.expected_rank_count ?? OutcomeKind.UNKNOWN} ranks`,
      installationId: item.installation_id,
    });

  const temperature = num(liveSample(node)?.gpu_temperature_c);
  if (temperature !== null) {
    const detail = `${temperature} °C`;
    if (temperature >= GPU_TEMP_SERIOUS_C)
      rows.push({ key: "gpu-heat", tone: "bad", title: "GPU temperature serious", detail });
    else if (temperature >= GPU_TEMP_WARN_C)
      rows.push({ key: "gpu-heat", tone: "warn", title: "GPU temperature high", detail });
    else rows.push({ key: "gpu-heat", tone: "ok", title: "GPU temperature normal", detail });
  }
  const memory = liveSample(node) ? nodeMemory(node) : null;
  if (memory) {
    const level = memoryLevel(memory.freeBytes);
    rows.push(
      level === "ok"
        ? {
            key: "memory",
            tone: "ok",
            title: "Memory headroom fine",
            detail: `${formatBytes(memory.freeBytes)} free`,
          }
        : {
            key: "memory",
            tone: level === "serious" ? "bad" : "warn",
            title: "Memory headroom low",
            detail: MEMORY_LOW_WORDS[level],
          },
    );
  }
  if (attentionUnavailable)
    rows.push({
      key: "alerts",
      tone: "neutral",
      title: "Alerts unavailable",
      detail: "retrying automatically",
    });
  else if (attention && alerts.length === 0)
    rows.push({ key: "alerts", tone: "ok", title: "No alerts firing", detail: "" });
  return rows.sort((a, b) => ORDER[a.tone] - ORDER[b.tone]);
}
