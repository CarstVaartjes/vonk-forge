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


def _metrics(args: argparse.Namespace, client: ControllerClient) -> dict:  # pyright: ignore[reportMissingTypeArgument]  # JSON document: the untyped-mapping guard refuses dict[str, object]
    if getattr(args, "metrics_action", None) != "show":
        return {"metrics": [spec.to_dict() for spec in client.metrics_catalogue()]}
    query: dict[str, str] = {"metric": args.metric, "range": args.range}
    names: dict[str, str] = {}
    if args.spark is not None:
        query["node"] = _resolve_spark_selectors(client, [args.spark])[0]
        names[query["node"]] = args.spark
    document = client.request("GET", "/api/metrics/series", query=query)
    response = MetricsSeriesResponse.from_dict(document)
    if not names and response.series:
        names = _spark_names(client)
    return {**document, "names": names}
