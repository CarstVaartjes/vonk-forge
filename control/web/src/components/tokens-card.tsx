import "./stat-card.css";
import type { MetricsSeriesResponse, VisualFleetNode } from "../api/types";
import { compactCount, modelTotals } from "../lib/metric-units";
import { Estimate, EstimateTag } from "./estimate";
import { Meter } from "./meter";

const NOT_REPORTED = "Not reported";

function count(value: number): string {
  const { figure, unit } = compactCount(value);
  return `${figure}${unit ? ` ${unit}` : ""}`;
}

/** The title of each loaded model by alias, for the line under the alias. */
function titles(nodes: VisualFleetNode[]): Map<string, string> {
  return new Map(
    nodes.flatMap((node) =>
      node.loaded.flatMap((run) =>
        run.alias && run.title ? [[run.alias, run.title] as const] : [],
      ),
    ),
  );
}

/**
 * Generated tokens over 24 hours: the total and one row per model with its share. The numbers are the
 * Controller's per-model totals, fleet total and shares. Prompt tokens and cache share
 * wait for their metric and say Not reported. The hint under the figure is the Controller's.
 */
export function TokensCard({
  tokens,
  nodes,
}: {
  tokens: MetricsSeriesResponse | null;
  nodes: VisualFleetNode[];
}) {
  const { rows: unsorted, sum, coverage } = modelTotals(tokens);
  const rows = [...unsorted].sort((a, b) => b.total - a.total);
  const names = titles(nodes);
  return (
    <section className="card stat-card" aria-labelledby="tokens-card-heading">
      <header>
        <h2 id="tokens-card-heading">Tokens</h2>
        <small>last 24 hours</small>
      </header>
      {tokens && rows.length > 0 ? (
        <>
          <p className="stat-fig">
            <Estimate basis={tokens.spec.basis}>{count(sum)}</Estimate>
            <small>generated</small>
            <EstimateTag basis={tokens.spec.basis} />
          </p>
          <p className="stat-caption">
            {tokens.spec.hint} Prompt tokens: {NOT_REPORTED}.
            {coverage < 1 && ` Data covers ${Math.floor(coverage * 100)}% of this range.`}
          </p>
          <div className="stat-table">
            <table>
              <caption className="sr-only">Generated tokens per model, last 24 hours</caption>
              <thead>
                <tr>
                  <th scope="col">Model</th>
                  <th scope="col">Generated</th>
                  <th scope="col">Share</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => {
                  const share = Math.round(100 * (row.share ?? 0));
                  return (
                    <tr key={row.alias}>
                      <th scope="row">
                        {row.alias}
                        {names.get(row.alias) && <small>{names.get(row.alias)}</small>}
                      </th>
                      <td>
                        <Estimate basis={tokens.spec.basis}>{count(row.total)}</Estimate>
                      </td>
                      <td>
                        <Meter
                          label={`${row.alias} share of generated tokens`}
                          vital={{ percent: share, text: `${share}%`, amber: false }}
                        />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      ) : tokens ? (
        <p className="stat-caption">No data in this range</p>
      ) : (
        <p className="stat-fig">{NOT_REPORTED}</p>
      )}
    </section>
  );
}
