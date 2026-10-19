"""One typed answer to "what is this operation waiting for?".

Every operation family (profile application, runtime image preparation, model
download) stores the same small list while it is waiting or blocked and shows it
unchanged in its own view, in Activity and in the evidence download.  The list
is a current snapshot, replaced at each check; it never accumulates history.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, TypeAdapter, ValidationError
from vonk_agent_protocol import (
    OperationReason,
    ProgressAlarmCode,
    RunSwitchCode,
    adopt_reason_code,
)
from vonk_agent_protocol.contracts import DigestText

from .strict_json import StrictJSONModel

MAX_BLOCKERS = 16
_BLOCKER_REASON = TypeAdapter(OperationReason | ProgressAlarmCode)

#: An operation is repeating a phase that failed (it is not merely waiting).
PHASE_RETRY_CODE = RunSwitchCode.PHASE_RETRY
#: A retry is a stall worth an operator's attention from this attempt on, so a
#: brief contention hold never raises one.
STALL_RETRY_ATTEMPT = 3

_NODE_PATTERN = re.compile(r"^spk_[0-9a-f]{32}$")
_NODE_ID = Annotated[str, StringConstraints(pattern=r"^spk_[0-9a-f]{32}$")]


class IdentityConflict(StrictJSONModel):
    """The two identities a conflict names, typed so evidence keeps them whole.

    ``authoritative`` is ``None`` when the owner holds no such identity.
    """

    identity: Literal["build_input_sha256", "execution_key"]
    submitted: DigestText
    authoritative: DigestText | None


class BlockingOperation(StrictJSONModel):
    """The operation a wait is behind, so a reader can follow it."""

    id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    kind: Annotated[str, StringConstraints(min_length=1, max_length=80)]


class StorageReliefEvidence(StrictJSONModel):
    """Free space and the collector's reclaim estimate for one node's demand."""

    required_free_bytes: int = Field(ge=0)
    available_bytes: int = Field(ge=0)
    reclaimable_bytes: int = Field(ge=0)


class OperationBlocker(StrictJSONModel):
    """One reason an operation is waiting or blocked, with the Sparks it concerns."""

    code: OperationReason | ProgressAlarmCode
    detail: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    severity: Literal["info", "warning", "error"]
    node_ids: list[_NODE_ID] = Field(default_factory=list, max_length=32)
    identity_conflict: IdentityConflict | None = None
    #: The operation this wait is behind, when one is known.
    operation: BlockingOperation | None = None
    storage: StorageReliefEvidence | None = None
    #: Exact workload whose effect the blocking operation owns, when present.
    run_id: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = (
        None
    )


class ProgressStall(StrictJSONModel):
    """The wait an operation keeps repeating, and how often it was seen."""

    signature: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    #: The leading blocker of the repeated wait, as the operation recorded it.
    detail: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    first_seen_at: datetime
    last_seen_at: datetime
    repeats: int = Field(ge=1)


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
        code=_BLOCKER_REASON.validate_json(json.dumps(code)),
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
        stored = dict(item)
        if isinstance(stored.get("code"), str):
            # A row written before a code was respelled reads as the current spelling.
            stored["code"] = adopt_reason_code(stored["code"])
        try:
            result.append(OperationBlocker.model_validate(stored))
        except ValidationError:
            continue
    return result[:MAX_BLOCKERS]


def dump_blockers(blockers: Iterable[OperationBlocker]) -> list[dict[str, object]]:
    return [item.model_dump(mode="json") for item in bound_blockers(blockers)]


__all__ = [
    "MAX_BLOCKERS",
    "PHASE_RETRY_CODE",
    "STALL_RETRY_ATTEMPT",
    "BlockingOperation",
    "IdentityConflict",
    "OperationBlocker",
    "ProgressAlarmCode",
    "ProgressStall",
    "StorageReliefEvidence",
    "bound_blockers",
    "dump_blockers",
    "make_blocker",
    "read_blockers",
]
