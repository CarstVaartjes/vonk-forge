import type { VisualFleetNode } from "../api/types";
import { hostCheckLines } from "../lib/host-checks";

/** Informative host advice from the Controller; never attention, and absent when there is none. */
export function SparkHostChecks({ node }: { node: VisualFleetNode }) {
  const checks = node.host_checks ?? [];
  if (checks.length === 0) return null;
  return (
    <section className="spark-host-checks" aria-labelledby={`host-checks-${node.id}`}>
      <h2 id={`host-checks-${node.id}`}>Host checks</h2>
      <ul>
        {checks.map((advice) => {
          const [text, ...commands] = hostCheckLines(advice);
          return (
            <li key={advice.check} data-level={advice.level}>
              <p>{text}</p>
              {commands.map((command) => (
                <pre key={command}>
                  <code>{command}</code>
                </pre>
              ))}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
