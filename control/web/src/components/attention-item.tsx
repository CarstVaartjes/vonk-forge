import type { VisualFleetNode, VisualFleetSnapshot } from "../api/types";
import { attentionText, attentionTone } from "../lib/fleet";
import { StatusPill } from "./status-pill";

/** One firing alert in plain words, `Severity: summary (Spark)`, as `vonkctl fleet` prints it. */
export function AttentionItem({
  alert,
  nodes,
}: {
  alert: NonNullable<VisualFleetSnapshot["attention"]>[number];
  nodes: VisualFleetNode[];
}) {
  const { severity, text } = attentionText(alert, nodes);
  return (
    <li>
      <StatusPill tone={attentionTone(alert)}>{severity}</StatusPill>: {text}
    </li>
  );
}
