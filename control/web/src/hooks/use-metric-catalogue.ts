import { useEffect, useState } from "react";
import type { ControlApi, MetricSpec } from "../api/types";
import { safeErrorText } from "../lib/error-display";

/**
 * What each metric is (title, unit, basis, hint), read from the Controller's catalogue: the list, or the
 * message of the read that failed, or undefined while it loads. Charts and figures take their words from
 * it; a metric the catalogue does not list is not drawn. Changing `retry` reads it again.
 */
export function useMetricCatalogue(
  api: Pick<ControlApi, "metricsCatalogue">,
  retry = 0,
): MetricSpec[] | string | undefined {
  const [catalogue, setCatalogue] = useState<MetricSpec[] | string>();
  useEffect(() => {
    const controller = new AbortController();
    setCatalogue(undefined);
    api.metricsCatalogue(controller.signal).then(
      (specs) => {
        if (!controller.signal.aborted) setCatalogue(specs);
      },
      (cause) => {
        if (!controller.signal.aborted)
          setCatalogue(
            safeErrorText(cause instanceof Error ? cause.message : "Metrics unavailable"),
          );
      },
    );
    return () => controller.abort();
  }, [api, retry]);
  return catalogue;
}
