"""Distribution: types."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from vonk_agent_protocol.compiled_execution_plan import CompiledExecutionPlan
from vonk_agent_protocol.contracts import canonical_message
from vonk_agent_protocol.distribution import DistributionObject
from vonk_agent_protocol.lifecycle_vocabulary import SecurityRefusalReason, WaitReason
from vonk_agent_protocol.outcome import SecurityRefusalError, UnknownOutcomeError


class DistributionError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class DistributionUnknown(UnknownOutcomeError, DistributionError):
    """Unavailable managed storage; the owner observes the same identity again."""

    def __init__(self, code: str, detail: str) -> None:
        DistributionError.__init__(self, code, detail)
        self.typed_reason = WaitReason.OBSERVATION_UNAVAILABLE


class DistributionRefused(SecurityRefusalError, DistributionError):
    """Denied source access or exact distribution authority; never a cache miss."""

    def __init__(
        self, code: str, detail: str, *, reason: SecurityRefusalReason
    ) -> None:
        DistributionError.__init__(self, code, detail)
        self.typed_reason = reason


class DistributionIntegrityError(DistributionRefused):
    """Content or immutable grant mismatch before publishing or serving bytes."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(code, detail, reason=SecurityRefusalReason.FORBIDDEN)


@dataclass(frozen=True, slots=True)
class OpenedObject:
    """An open stored object whose name is its content address and whose size was checked."""

    stream: BinaryIO
    size: int
    sha256: str
    # Where the object sits in Controller storage. The edge serves the bytes
    # from this path once the Controller has authorized the request.
    path: Path


class ObjectSource(Protocol):
    def open_object(self, digest: str, expected_bytes: int) -> OpenedObject:
        """Open a complete immutable object or raise DistributionError."""
        ...

    def verify_artifact_set(
        self, artifact_set_sha256: str, objects: tuple[DistributionObject, ...]
    ) -> bool:
        """Prove that the exact model objects belong to the cache manifest."""
        ...

    def verify_runtime_image(self, image_digest: str, archive_sha256: str) -> bool:
        """Prove the archive is the exact OCI image selected by the plan."""
        ...


def artifact_set_sha256(objects: tuple[DistributionObject, ...]) -> str:
    """Return the digest used by cache adapters for an exact model manifest."""
    model_objects = [item.to_mapping() for item in objects if item.kind == "model"]
    return hashlib.sha256(canonical_message(model_objects)).hexdigest()


_artifact_set_digest = artifact_set_sha256


def content_objects(
    objects: tuple[DistributionObject, ...],
) -> tuple[DistributionObject, ...]:
    """The model objects as a node fetches them: each content digest once.

    A set may hold identical content at several paths; nodes store objects by
    digest, so an assignment carries the first path of each digest.  Building
    an assignment and verifying one use this same projection.
    """
    unique: dict[str, DistributionObject] = {}
    for item in objects:
        if item.kind == "model":
            unique.setdefault(item.sha256, item)
    return tuple(unique.values())


def contained_content(
    stored: tuple[DistributionObject, ...], objects: tuple[DistributionObject, ...]
) -> bool:
    """Whether every object is content the stored set holds (digest and length).

    A node's plan names its role's files; the stored set holds every role's,
    so the stored set is the superset and the plan is the exact selection.
    """
    held = {item.sha256: item.bytes for item in content_objects(stored)}
    return bool(objects) and all(
        held.get(item.sha256) == item.bytes for item in objects
    )


def plan_objects(plan: CompiledExecutionPlan) -> tuple[DistributionObject, ...]:
    """The model objects an accepted compiled plan names, each digest once.

    Compilation binds the plan to its model set's receipts and covers exactly
    that set, so a grant built from it needs no NAS copy. A node fetches only
    the objects it lacks; serving one still reads the NAS set.
    """
    return content_objects(
        tuple(
            DistributionObject(
                name=item.path, sha256=item.sha256, bytes=item.size_bytes, kind="model"
            )
            for item in plan.artifacts
        )
    )
