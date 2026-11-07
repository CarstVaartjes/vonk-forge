"""Bounded resolution of model, recipe, and Spark selectors."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from ..cli_select import SelectorError
from ..control_client import (
    ControlClientError,
    ControlForbidden,
    ControlMalformedResponse,
    ControlUnauthorized,
    validate_control_document,
)

if TYPE_CHECKING:
    from ..generated_control.models.library_recipe_projection import (
        LibraryRecipeProjection,
    )

from .common import ControllerClient
from .submission import _known_http_refusal_status


def _selection_remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ControlMalformedResponse("selection observation deadline reached")
    return remaining


def _observe_selection[T](read: Callable[[], T], *, deadline: float) -> T:
    """Restart incomplete reads; only a complete observation can resolve a name."""
    last_error: Exception | None = None
    for attempt in range(3):
        _selection_remaining(deadline)
        try:
            result = read()
            _selection_remaining(deadline)
            return result
        except (ControlForbidden, ControlUnauthorized):
            raise
        except (ControlClientError, OSError, TypeError, ValueError) as error:
            if isinstance(error, ControlClientError) and _known_http_refusal_status(
                error
            ) in {401, 403}:
                raise
            last_error = error
            if attempt == 2 or time.monotonic() >= deadline:
                break
            time.sleep(min(0.1 * 2**attempt, max(0, deadline - time.monotonic())))
    cause = f"{type(last_error).__name__}: {last_error}"[:300]
    raise ControlMalformedResponse(
        f"selection observation is unavailable; last error {cause}"
    ) from last_error


@dataclass(frozen=True)
class _ReadOutcome:
    """One read's own outcome: its value or the error it ended with."""

    value: object = None
    error: Exception | None = None

    def get(self) -> object:
        """The read's value; raises that read's own error."""
        if self.error is not None:
            raise self.error
        return self.value


_MAX_CONCURRENT_READS = 3


def _skipped_read() -> None:
    """A read that is not needed for this action."""


def _run_read(read: Callable[[], object]) -> _ReadOutcome:
    try:
        return _ReadOutcome(value=read())
    except Exception as error:  # noqa: BLE001 - recorded as this read's outcome
        return _ReadOutcome(error=error)


def _read_concurrently(*reads: Callable[[], object]) -> list[_ReadOutcome]:
    """Run independent reads at once; each keeps its own outcome.

    Every read caps itself at its own deadline, so this helper never invents
    an error or a timeout for a read: it waits until each read has finished
    and returns that read's own value or error. The outcomes come back in the
    order given, so a caller that consumes them in the order it used to run
    the reads meets the same first error as before.

    A single real read runs inline. Several run in daemon threads while the
    caller waits on each read's result in order; that wait is interruptible, so Ctrl-C
    still raises KeyboardInterrupt on the main thread at once.
    """
    real = [read for read in reads if read is not _skipped_read]
    if len(real) > _MAX_CONCURRENT_READS:
        raise ValueError("too many concurrent reads")
    if len(real) <= 1:
        return [
            _ReadOutcome() if read is _skipped_read else _run_read(read)
            for read in reads
        ]
    pending: dict[int, Future[_ReadOutcome]] = {
        index: Future() for index, read in enumerate(reads) if read is not _skipped_read
    }

    def work(future: Future[_ReadOutcome], read: Callable[[], object]) -> None:
        future.set_result(_run_read(read))

    for index, future in pending.items():
        threading.Thread(target=work, args=(future, reads[index]), daemon=True).start()
    return [
        _ReadOutcome() if index not in pending else pending[index].result()
        for index in range(len(reads))
    ]


def _recipe_rows(
    client: ControllerClient, *, deadline: float
) -> Iterator[Mapping[str, object]]:
    rows = _observe_selection(
        lambda: list(_scan_recipe_rows(client, deadline=deadline)), deadline=deadline
    )
    for row in rows:
        yield row.to_dict()


def _scan_recipe_rows(
    client: ControllerClient, *, deadline: float
) -> Iterator[LibraryRecipeProjection]:
    """Decode a complete catalogue read before using any partial matches."""
    from ..generated_control.models.library_recipe_projection import (
        LibraryRecipeProjection,
    )

    seen_selectors: set[str] = set()
    for raw_recipes in _library_pages(
        client,
        "/api/recipe/library",
        "recipes",
        {"limit": 512, "sort": "name", "assess": False},
        deadline=deadline,
    ):
        for row in raw_recipes:
            _selection_remaining(deadline)
            if not isinstance(row, Mapping) or not isinstance(
                row.get("identity"), Mapping
            ):
                raise ControlMalformedResponse(
                    "recipe library response contains an invalid recipe"
                )
            selector = row.get("selector")
            if not isinstance(selector, str) or not selector:
                raise ControlMalformedResponse(
                    "recipe library response contains an invalid selector"
                )
            if selector.casefold() in seen_selectors:
                raise ControlMalformedResponse("recipe library repeated a selector")
            decoded = LibraryRecipeProjection.from_dict(
                validate_control_document("LibraryRecipeProjection", row)
            )
            seen_selectors.add(selector.casefold())
            yield decoded


def _library_pages(
    client: ControllerClient,
    path: str,
    key: str,
    query: Mapping,
    *,
    deadline: float,
) -> Iterator[list[object]]:
    """The item list of every page of a cursor-paged library read, in order.

    The read ends with the Controller's last page; a repeated or malformed
    cursor is a malformed response, so the loop is bounded by the data.
    """
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        page = client.request(
            "GET",
            path,
            query=query if cursor is None else {**query, "cursor": cursor},
            timeout_seconds=_selection_remaining(deadline),
        )
        _selection_remaining(deadline)
        items = page.get(key)
        if not isinstance(items, list):
            raise ControlMalformedResponse(f"{path} response does not contain {key}")
        yield items
        next_cursor = page.get("next_cursor")
        if next_cursor is None:
            return
        if not isinstance(next_cursor, str) or not next_cursor:
            raise ControlMalformedResponse(
                f"{path} response contains an invalid cursor"
            )
        if next_cursor in seen_cursors:
            raise ControlMalformedResponse(f"{path} cursor repeated")
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
    if re.fullmatch(
        r"[0-9a-f]{64}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[A-Za-z0-9._-]+/[A-Za-z0-9._-]+",
        requested,
    ):
        return requested
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

        def owner_detail():
            from .common import _quoted

            detail = client.request(
                "GET",
                f"/api/recipe/{_quoted(requested)}",
                timeout_seconds=_selection_remaining(deadline),
            )
            selector = detail.get("selector")
            if not isinstance(selector, str) or not selector:
                raise ControlMalformedResponse("recipe identity is not yet observed")
            return selector

        return _observe_selection(owner_detail, deadline=deadline)
    candidates = tuple(sorted(matches))
    raise SelectorError(
        f"ambiguous recipe selector: {requested}",
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
    if any(not value.strip() for value in selectors):
        raise SelectorError("spark selector cannot be empty")
    if all(re.fullmatch(r"spk_[0-9a-f]{32}", value) for value in selectors):
        return list(dict.fromkeys(selectors))

    def roster():
        payload = client.request(
            "GET", "/api/fleet", timeout_seconds=_selection_remaining(deadline)
        )
        raw_nodes = payload.get("nodes")
        if not isinstance(raw_nodes, list):
            raise ControlMalformedResponse("fleet nodes are unavailable")
        ids: set[str] = set()
        for node in raw_nodes:
            if not isinstance(node, Mapping):
                raise ControlMalformedResponse("fleet contains an unreadable Spark")
            node_id = node.get("id")
            if not isinstance(node_id, str) or not node_id or node_id in ids:
                raise ControlMalformedResponse(
                    "fleet contains an unreadable Spark identity"
                )
            ids.add(node_id)
        for requested in selectors:
            needle = requested.strip().casefold()
            if not needle:
                raise SelectorError("spark selector cannot be empty")
            if re.fullmatch(r"spk_[0-9a-f]{32}", requested):
                continue
            if not any(
                any(
                    isinstance(value, str) and value.casefold() == needle
                    for value in (
                        node.get("id"),
                        node.get("display_name"),
                        node.get("hostname"),
                    )
                )
                for node in raw_nodes
            ):
                raise ControlMalformedResponse("Spark membership is not yet observed")
        return raw_nodes

    nodes = _observe_selection(roster, deadline=deadline)
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
            if re.fullmatch(r"spk_[0-9a-f]{32}", requested):
                resolved.append(requested)
                continue
            raise ControlMalformedResponse("Spark membership is not yet observed")
        if len(matches) > 1:
            candidates = tuple(sorted(str(node["id"]) for node in matches))
            raise SelectorError(
                f"ambiguous spark selector: {requested}",
                candidates=candidates,
            )
        resolved.append(cast(str, matches[0]["id"]))
    _selection_remaining(deadline)
    return sorted(set(resolved))
