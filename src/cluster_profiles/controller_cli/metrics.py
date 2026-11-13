"""`vonkctl metrics`: the Controller's metric catalogue and one metric's series."""

from __future__ import annotations

import argparse
from collections.abc import Mapping

from ..control_client import ControlClientError
from ..generated_control.models.metrics_series_response import MetricsSeriesResponse
from .common import ControllerClient
from .selection import _resolve_spark_selectors


def _metrics(args: argparse.Namespace, client: ControllerClient) -> dict:
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


def _spark_names(client: ControllerClient) -> dict[str, str]:
    """Friendly names from one roster read; ids stand in when it fails."""
    try:
        nodes = client.request("GET", "/api/fleet").get("nodes")
    except ControlClientError:
        return {}
    if not isinstance(nodes, list):
        return {}
    return {
        node["id"]: node["display_name"]
        for node in nodes
        if isinstance(node, Mapping)
        and isinstance(node.get("id"), str)
        and isinstance(node.get("display_name"), str)
    }
