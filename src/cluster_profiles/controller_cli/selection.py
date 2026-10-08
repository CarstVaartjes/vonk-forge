"""Bounded resolution of model, recipe, and Spark selectors."""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping
from typing import cast

from ..cli_select import SelectorError
from .common import ControllerClient


def _selection_remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SelectorError(
            "selection deadline expired; nothing was saved. Retry with a larger "
            "--timeout-seconds value (maximum 300)."
        )
    return remaining


def _recipe_rows(
    client: ControllerClient, *, deadline: float
) -> Iterator[Mapping[str, object]]:
    """Read every page within one budget, without retaining the full catalog."""

    cursor: str | None = None
    seen_cursors: set[str] = set()
    seen_selectors: set[str] = set()
    while True:
        query: dict[str, object] = {
            "limit": 512,
            "sort": "name",
            "assess": False,
        }
        if cursor is not None:
            query["cursor"] = cursor
        payload = client.request(
            "GET",
            "/api/recipe/library",
            query=query,
            timeout_seconds=_selection_remaining(deadline),
        )
        _selection_remaining(deadline)
        raw_recipes = payload.get("recipes")
        if not isinstance(raw_recipes, list):
            raise TypeError("recipe library response does not contain recipes")
        for row in raw_recipes:
            _selection_remaining(deadline)
            if not isinstance(row, Mapping) or not isinstance(
                row.get("identity"), Mapping
            ):
                raise TypeError("recipe library response contains an invalid recipe")
            selector = row.get("selector")
            if not isinstance(selector, str) or not selector:
                raise ValueError("recipe library response contains an invalid selector")
            if selector.casefold() in seen_selectors:
                raise SelectorError(
                    "recipe library repeated a selector; retry the complete selection"
                )
            seen_selectors.add(selector.casefold())
            yield row
        next_cursor = payload.get("next_cursor")
        if next_cursor is None:
            return
        if not isinstance(next_cursor, str) or not next_cursor:
            raise TypeError("recipe library response contains an invalid cursor")
        if next_cursor in seen_cursors:
            raise SelectorError(
                "recipe library cursor repeated; selection stopped without saving"
            )
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def _resolve_recipe_selector(
    client: ControllerClient, requested: str, *, deadline: float | None = None
) -> str:
    """Resolve an accepted identity before considering a friendly slug or title."""

    needle = requested.strip().casefold()
    if not needle:
        raise SelectorError("recipe selector cannot be empty")
    if deadline is None:
        deadline = time.monotonic() + 30
    identities: set[str] = set()
    matches: set[str] = set()
    for row in _recipe_rows(client, deadline=deadline):
        identity = cast(Mapping[str, object], row["identity"])
        canonical = cast(str, row["selector"])
        if any(
            isinstance(value, str) and value.casefold() == needle
            for value in (canonical, identity.get("recipe_id"))
        ):
            identities.add(canonical)
        if any(
            isinstance(value, str) and value.casefold() == needle
            for value in (identity.get("slug"), identity.get("title"))
        ):
            matches.add(canonical)
    _selection_remaining(deadline)
    matches = identities or matches
    if len(matches) == 1:
        return next(iter(matches))
    if not matches:
        raise SelectorError(f"unknown recipe selector: {requested}")
    candidates = tuple(sorted(matches))
    raise SelectorError(
        f"ambiguous recipe selector: {requested}; choose an exact canonical candidate",
        candidates=candidates,
    )


def _resolve_spark_selectors(
    client: ControllerClient, selectors: list[str], *, deadline: float | None = None
) -> list[str]:
    """Resolve operator-facing Spark names to immutable node IDs."""

    if not selectors:
        return []
    if deadline is None:
        deadline = time.monotonic() + 30
    payload = client.request(
        "GET", "/api/fleet", timeout_seconds=_selection_remaining(deadline)
    )
    _selection_remaining(deadline)
    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, list):
        raise TypeError("fleet response does not contain nodes")
    nodes: list[Mapping[str, object]] = []
    ids: set[str] = set()
    for node in raw_nodes:
        if not isinstance(node, Mapping):
            raise TypeError("fleet response contains an invalid Spark")
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id or node_id in ids:
            raise ValueError("fleet response contains an invalid or repeated Spark ID")
        ids.add(node_id)
        nodes.append(node)
    resolved: list[str] = []
    for requested in selectors:
        needle = requested.strip().casefold()
        if not needle:
            raise SelectorError("spark selector cannot be empty")
        exact = [node for node in nodes if str(node["id"]).casefold() == needle]
        matches = exact or [
            node
            for node in nodes
            if any(
                isinstance(value, str) and value.casefold() == needle
                for key in ("display_name", "hostname")
                for value in (node.get(key),)
            )
        ]
        if not matches:
            raise SelectorError(f"unknown spark selector: {requested}")
        if len(matches) > 1:
            candidates = tuple(sorted(str(node["id"]) for node in matches))
            raise SelectorError(
                f"ambiguous spark selector: {requested}; choose an exact Spark ID",
                candidates=candidates,
            )
        resolved.append(cast(str, matches[0]["id"]))
    _selection_remaining(deadline)
    return sorted(set(resolved))
