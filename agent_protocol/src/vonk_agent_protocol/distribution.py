"""Exact, assignment-bound Controller distribution messages.

The distribution API is deliberately separate from the claim payload.  A claim
or install plan may reference these immutable objects, while the mTLS agent
fetches the bytes with the assignment and generation on every request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from .contracts import AgentProtocolError, _fields, _mapping, _uuid

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_OCI_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_NODE = re.compile(r"spk_[0-9a-f]{32}\Z")
_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/-]{0,511}\Z")
_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _digest(value: Any, name: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise AgentProtocolError(f"{name} must be a lowercase SHA-256")
    return value


def _bytes(value: Any, name: str, *, positive: bool = False) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < (1 if positive else 0)
        or value > 16 * 1024**4
    ):
        raise AgentProtocolError(f"{name} is invalid")
    return value


def _utc(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise AgentProtocolError(f"{name} is invalid")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as error:
        raise AgentProtocolError(f"{name} is invalid") from error
    if result.tzinfo is None or result.utcoffset() != UTC.utcoffset(result):
        raise AgentProtocolError(f"{name} must be UTC")
    return result


def _name(value: Any, name: str) -> str:
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        raise AgentProtocolError(f"{name} is invalid")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise AgentProtocolError(f"{name} is unsafe")
    return value


@dataclass(frozen=True, slots=True)
class DistributionObject:
    """One immutable model file, OCI archive, or OCI layer."""

    name: str
    sha256: str
    bytes: int
    kind: str

    @classmethod
    def parse(cls, value: Any) -> DistributionObject:
        item = _mapping(value)
        _fields(item, required={"name", "sha256", "bytes", "kind"})
        kind = item["kind"]
        if kind not in {"model", "oci-archive", "oci-layer"}:
            raise AgentProtocolError("distribution object kind is invalid")
        size = item["bytes"]
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or size > 16 * 1024**4
            or (size == 0 and (kind != "model" or item["sha256"] != _EMPTY_SHA256))
        ):
            raise AgentProtocolError("distribution object bytes are invalid")
        return cls(
            name=_name(item["name"], "distribution object name"),
            sha256=_digest(item["sha256"], "distribution object sha256"),
            bytes=size,
            kind=kind,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "name": self.name,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "kind": self.kind,
        }


@dataclass(frozen=True, slots=True)
class DistributionAssignment:
    """Controller-issued authorization for one exact node installation."""

    schema_version: int
    assignment_id: str
    plan_digest: str
    generation: int
    node_id: str
    expires_at: datetime
    model_artifact_set_sha256: str
    objects: tuple[DistributionObject, ...]
    oci_image_digest: str
    oci_archive_sha256: str

    @classmethod
    def parse(cls, value: Any) -> DistributionAssignment:
        item = _mapping(value)
        required = {
            "schema_version", "assignment_id", "plan_digest", "generation",
            "node_id", "expires_at", "model_artifact_set_sha256", "objects",
            "oci_image_digest", "oci_archive_sha256",
        }
        _fields(item, required=required)
        if item["schema_version"] != 2 or not isinstance(item["generation"], int) or item["generation"] < 1:
            raise AgentProtocolError("distribution assignment version or generation is invalid")
        node_id = item["node_id"]
        if not isinstance(node_id, str) or _NODE.fullmatch(node_id) is None:
            raise AgentProtocolError("distribution assignment node is invalid")
        raw_objects = item["objects"]
        if not isinstance(raw_objects, list) or not 1 <= len(raw_objects) <= 4096:
            raise AgentProtocolError("distribution assignment objects are invalid")
        objects = tuple(DistributionObject.parse(raw) for raw in raw_objects)
        if len({obj.sha256 for obj in objects}) != len(objects):
            raise AgentProtocolError("distribution assignment objects are duplicated")
        if not any(obj.kind == "model" for obj in objects):
            raise AgentProtocolError("distribution assignment has no model objects")
        if not any(obj.sha256 == item["oci_archive_sha256"] and obj.kind == "oci-archive" for obj in objects):
            raise AgentProtocolError("distribution assignment OCI archive is not declared")
        assignment_id = _uuid(item["assignment_id"], name="assignment_id")
        if UUID(assignment_id).version != 4:
            raise AgentProtocolError("assignment_id must be a random UUID")
        image_digest = item["oci_image_digest"]
        if not isinstance(image_digest, str) or _OCI_DIGEST.fullmatch(image_digest) is None:
            raise AgentProtocolError("oci image digest is invalid")
        return cls(
            schema_version=2,
            assignment_id=assignment_id,
            plan_digest=_digest(item["plan_digest"], "plan_digest"),
            generation=item["generation"],
            node_id=node_id,
            expires_at=_utc(item["expires_at"], "expires_at"),
            model_artifact_set_sha256=_digest(item["model_artifact_set_sha256"], "model_artifact_set_sha256"),
            objects=objects,
            oci_image_digest=image_digest,
            oci_archive_sha256=_digest(item["oci_archive_sha256"], "oci_archive_sha256"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "assignment_id": self.assignment_id,
            "plan_digest": self.plan_digest,
            "generation": self.generation,
            "node_id": self.node_id,
            "expires_at": self.expires_at.isoformat(),
            "model_artifact_set_sha256": self.model_artifact_set_sha256,
            "objects": [obj.to_mapping() for obj in self.objects],
            "oci_image_digest": self.oci_image_digest,
            "oci_archive_sha256": self.oci_archive_sha256,
        }

    @property
    def object_digests(self) -> frozenset[str]:
        return frozenset(item.sha256 for item in self.objects)


__all__ = [
    "DistributionAssignment",
    "DistributionObject",
]
