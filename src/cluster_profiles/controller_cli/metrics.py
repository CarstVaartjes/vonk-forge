"""`vonkctl metrics`: the Controller's metric catalogue and one metric's series."""

from __future__ import annotations

import argparse
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..control_client import ControlClientError
from ..generated_control.models.metrics_series_response import MetricsSeriesResponse
from .common import ControllerClient
from .selection import _resolve_spark_selectors

if TYPE_CHECKING:
    from ..generated_control.models.metric_spec import MetricSpec

_SPARK_ID = re.compile(r"spk_[0-9a-f]{32}")


@runtime_checkable
class _CatalogueReader(Protocol):
    """The catalogue is a JSON list, which `request` (documents only) cannot return."""

    def metrics_catalogue(self) -> list[MetricSpec]: ...


def _metrics(args: argparse.Namespace, client: ControllerClient) -> dict:
    if getattr(args, "metrics_action", None) != "show":
        if not isinstance(client, _CatalogueReader):
            raise ControlClientError("this client cannot read the metric catalogue")
        return {"metrics": [spec.to_dict() for spec in client.metrics_catalogue()]}
    query: dict[str, str] = {"metric": args.metric, "range": args.range}
    if args.spark is not None:
        query["node"] = _resolve_spark_selectors(client, [args.spark])[0]
    document = client.request("GET", "/api/metrics/series", query=query)
    response = MetricsSeriesResponse.from_dict(document)
    return {**document, "names": _spark_names(client, response)}


def _spark_names(
    client: ControllerClient, response: MetricsSeriesResponse
) -> dict[str, str]:
    """Friendly names for the Sparks a series is labelled with; ids stand in without them."""
    labelled = {
        value
        for series in response.series
        for value in series.labels.additional_properties.values()
        if _SPARK_ID.fullmatch(value)
    }
    if not labelled:
        return {}
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
        and node.get("id") in labelled
        and isinstance(node.get("display_name"), str)
    }
