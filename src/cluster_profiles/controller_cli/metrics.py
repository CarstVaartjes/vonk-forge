"""`vonkctl metrics`: the Controller's metric catalogue and one metric's series."""

from __future__ import annotations

import argparse

from cluster_profiles.controller_cli.common import ControllerClient
from cluster_profiles.controller_cli.selection import (
    _resolve_spark_selectors,
    _spark_names,
)
from cluster_profiles.generated_control.models.metrics_series_response import (
    MetricsSeriesResponse,
)


def _metric_series(
    args: argparse.Namespace, client: ControllerClient
) -> tuple[MetricsSeriesResponse, dict[str, str]]:
    """One metric's series and the display names of the Sparks it shows."""
    query: dict[str, str] = {"metric": args.metric, "range": args.range}
    names: dict[str, str] = {}
    if args.spark is not None:
        query["node"] = _resolve_spark_selectors(client, [args.spark])[0]
        names[query["node"]] = args.spark
    response = MetricsSeriesResponse.from_dict(
        client.request("GET", "/api/metrics/series", query=query)
    )
    if not names and response.series:
        names = _spark_names(client)
    return response, names
