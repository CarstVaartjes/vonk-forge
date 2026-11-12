import { useEffect, useState } from "react";
import { EndpointState, ProfileDocumentState } from "../api/vocabulary.generated";
import {
  readableProfile,
  type ControlApi,
  type FleetProfileEndpoints,
  type VisualFleetNode,
} from "../api/types";
import { ConnectionExamples } from "./connection-examples";

type ServedAssignment = NonNullable<FleetProfileEndpoints["assignments"]>[number];

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
  const [serving, setServing] = useState<ServedAssignment[] | null>([]);
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
      return (endpoints.assignments ?? []).filter(
        (item) => item.state === EndpointState.PUBLISHED && item.alias && item.endpoint,
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
          <li key={item.assignment_id}>
            <p>
              <strong>
                <code>{item.alias}</code>
              </strong>{" "}
              · {item.recipe_title} · {item.state}
            </p>
            <details>
              <summary>Show connection example</summary>
              {item.endpoint && item.alias && (
                <ConnectionExamples apiBase={item.endpoint.api_base} alias={item.alias} />
              )}
            </details>
          </li>
        ))}
      </ul>
    </section>
  );
}
