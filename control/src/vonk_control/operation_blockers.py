"""One typed answer to "what is this operation waiting for?".

Every operation family (profile application, runtime image preparation, model
download) stores the same small list while it is waiting or blocked and shows it
unchanged in its own view, in Activity and in the evidence download.  The list
is a current snapshot, replaced at each check; it never accumulates history.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, ValidationError

from .strict_json import StrictJSONModel

MAX_BLOCKERS = 16

_NODE_PATTERN = re.compile(r"^spk_[0-9a-f]{32}$")
_NODE_ID = Annotated[str, StringConstraints(pattern=r"^spk_[0-9a-f]{32}$")]


class OperationBlocker(StrictJSONModel):
    """One reason an operation is waiting or blocked, with the Sparks it concerns."""

    code: Annotated[str, StringConstraints(min_length=1, max_length=96)]
    detail: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    severity: Literal["info", "warning", "error"]
    node_ids: list[_NODE_ID] = Field(default_factory=list, max_length=32)


def make_blocker(
    code: str,
    detail: str,
    *,
    severity: Literal["info", "warning", "error"] = "warning",
    node_ids: Iterable[str] = (),
) -> OperationBlocker:
    """Build a blocker from free text, bounding it instead of failing on length."""

    nodes = sorted({node for node in node_ids if _NODE_PATTERN.fullmatch(node)})
    return OperationBlocker(
        code=(code or "waiting")[:96],
        detail=(detail or code or "waiting")[:512],
        severity=severity,
        node_ids=nodes[:32],
    )


def bound_blockers(blockers: Iterable[OperationBlocker]) -> list[OperationBlocker]:
    """Drop repeats and keep at most :data:`MAX_BLOCKERS`, errors first."""

    seen: set[tuple[str, tuple[str, ...]]] = set()
    unique: list[OperationBlocker] = []
    for blocker in blockers:
        key = (blocker.code, tuple(blocker.node_ids))
        if key not in seen:
            seen.add(key)
            unique.append(blocker)
    rank = {"error": 0, "warning": 1, "info": 2}
    unique.sort(key=lambda item: rank[item.severity])
    return unique[:MAX_BLOCKERS]


def read_blockers(value: object) -> list[OperationBlocker]:
    """Read a stored blocker list; an unreadable entry is dropped, never fatal."""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    result: list[OperationBlocker] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        try:
            result.append(OperationBlocker.model_validate(dict(item)))
        except ValidationError:
            continue
    return result[:MAX_BLOCKERS]


def dump_blockers(blockers: Iterable[OperationBlocker]) -> list[dict[str, object]]:
    return [item.model_dump(mode="json") for item in bound_blockers(blockers)]


__all__ = [
    "MAX_BLOCKERS",
    "OperationBlocker",
    "bound_blockers",
    "dump_blockers",
    "make_blocker",
    "read_blockers",
]
