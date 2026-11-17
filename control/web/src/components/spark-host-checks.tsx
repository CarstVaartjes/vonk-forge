import type { VisualFleetNode } from "../api/types";
import { hostCheckLines, UNDO_LABEL } from "../lib/host-checks";
import { CopyButton } from "./copy-button";

/** Informative host advice from the Controller; never attention, and absent when there is none. */
export function SparkHostChecks({ node }: { node: VisualFleetNode }) {
  const checks = node.host_checks ?? [];
  if (checks.length === 0) return null;
  return (
    <section className="spark-host-checks" aria-labelledby={`host-checks-${node.id}`}>
      <h2 id={`host-checks-${node.id}`}>Host checks</h2>
      {node.host_checks_note && <p>{noteWithLink(node.host_checks_note)}</p>}
      <ul>
        {checks.map((advice) => {
          const [text, ...commands] = hostCheckLines(advice);
          return (
            <li key={advice.check}>
              <p>{text}</p>
              {commands.map((command) =>
                command === UNDO_LABEL ? (
                  <p key={command}>
                    <strong>{command}</strong>
                  </p>
                ) : (
                  <div className="host-check-command" key={command}>
                    <pre>
                      <code>{command}</code>
                    </pre>
                    <CopyButton label="command" value={command} />
                  </div>
                ),
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/** The Controller's note with its docs URL as a link; the text is unchanged. */
function noteWithLink(note: string) {
  const match = /https?:\/\/\S+$/.exec(note);
  if (!match) return note;
  return (
    <>
      {note.slice(0, match.index)}
      <a href={match[0]} target="_blank" rel="noreferrer">
        {match[0]}
      </a>
    </>
  );
}
