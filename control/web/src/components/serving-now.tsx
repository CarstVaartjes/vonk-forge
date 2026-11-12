import { useEffect, useState } from "react";
import { EndpointState, ProfileDocumentState } from "../api/vocabulary.generated";
import { readableProfile, type ControlApi, type VisualFleetNode } from "../api/types";
import { ConnectionExamples } from "./connection-examples";

type ServedAlias = { id: string; alias: string; title: string; state: string; apiBase: string };

/** Changes when a workload starts, stops or changes health, so the list follows the fleet. */
export function servingKey(nodes: readonly VisualFleetNode[]): string {
  return nodes
    .flatMap((node) => node.loaded.map((run) => `${run.run_id}:${run.rank}:${run.healthy}`))
    .sort()
    .join(",");
}

/**
 * What the loaded profile serves right now, one line per published alias, with the connection
 * example for each. Everything comes from the Controller's profile and endpoint views; the browser
 * never calls the inference gateway. `refreshKey` changes when the fleet's workloads change.
 */
export function ServingNow({
  api,
  refreshKey,
}: {
  api: Pick<ControlApi, "profiles" | "profileEndpoints">;
  refreshKey: string;
}) {
  // null: the read failed, which is not the same as nothing being served
  const [serving, setServing] = useState<ServedAlias[] | null>([]);
  useEffect(() => {
    void refreshKey;
    const controller = new AbortController();
    const { signal } = controller;
    void (async () => {
      const list = await api.profiles(signal);
      const loaded = list.profiles
        .filter(readableProfile)
        .find((profile) => profile.status === ProfileDocumentState.LOADED);
      if (!loaded) return [];
      const endpoints = await api.profileEndpoints(loaded.number, signal);
      return (endpoints.assignments ?? []).flatMap((item) =>
        item.state === EndpointState.PUBLISHED && item.alias && item.endpoint
          ? [
              {
                id: item.assignment_id,
                alias: item.alias,
                title: item.recipe_title,
                state: item.state,
                apiBase: item.endpoint.api_base,
              },
            ]
          : [],
      );
    })().then(
      (value) => {
        if (!signal.aborted) setServing(value);
      },
      () => {
        if (!signal.aborted) setServing(null);
      },
    );
    return () => controller.abort();
  }, [api, refreshKey]);
  if (serving === null)
    return (
      <p className="serving-now" role="status">
        What is serving now is not available right now.
      </p>
    );
  if (serving.length === 0) return null;
  return (
    <section className="serving-now" aria-labelledby="serving-now-heading">
      <h2 id="serving-now-heading">Serving now</h2>
      <ul>
        {serving.map((item) => (
          <li key={item.id}>
            <p>
              <strong>
                <code>{item.alias}</code>
              </strong>{" "}
              · {item.title} · {item.state}
            </p>
            <details>
              <summary>Show connection example</summary>
              <ConnectionExamples apiBase={item.apiBase} alias={item.alias} />
            </details>
          </li>
        ))}
      </ul>
    </section>
  );
}
