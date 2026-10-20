"""Reviewed, content-addressed effects of a request to remove a fleet model."""

from datetime import timedelta
from enum import StrEnum
from typing import Annotated

from pydantic import AwareDatetime, Field
from vonk_agent_protocol.inventory import ModelStoreObject

from .lifecycle.evidence import BookkeepingReason
from .strict_json import StrictJSONModel

STORE_RETENTION_MAX_AGE = timedelta(minutes=10)
FLEET_REMOVAL_DEADLINE = timedelta(hours=24)

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class FleetRemovalDisposition(StrEnum):
    REMOVE = "remove"
    RETAIN = "retain"


class FleetRemovalReason(StrEnum):
    UNUSED = "unused"
    REFERENCED = "referenced"
    SHARED = "shared"
    OBSERVATION_UNAVAILABLE = "observation-unavailable"


class FleetRemovalObservationIssue(StrictJSONModel):
    """Incomplete owner evidence; never proof of absence or removal authority."""

    kind: str
    subject: str
    reason: BookkeepingReason
    detail: str


class FleetRemovalObject(StrictJSONModel):
    object: ModelStoreObject
    disposition: FleetRemovalDisposition
    reason: FleetRemovalReason
    detail: str


class FleetRemovalInstallation(StrictJSONModel):
    installation_id: str
    plan_digest: Digest | None = None
    disposition: FleetRemovalDisposition
    reason: FleetRemovalReason
    detail: str


class FleetRemovalTarget(StrictJSONModel):
    node_id: str
    objects: list[FleetRemovalObject]
    installations: list[FleetRemovalInstallation]
    observation_available: bool
    reclaimable_bytes: int | None = Field(
        default=None,
        ge=0,
        description="Known model-store file bytes. Additional installation cleanup bytes are unknown because links can be shared.",
    )


class FleetModelRemovalEffects(StrictJSONModel):
    nas_remove: bool
    nas_reason: FleetRemovalReason
    nas_detail: str
    targets: list[FleetRemovalTarget]
    observation_issues: list[FleetRemovalObservationIssue] = Field(
        default_factory=list, exclude_if=lambda value: not value
    )


class FleetModelRemovalCheckpoint(StrictJSONModel):
    deadline_at: AwareDatetime
    effects: FleetModelRemovalEffects
    # The accepted review remains immutable. Only exact child identities and
    # confirmations advance; a response loss replays the same UUID5 child.
    completed_installations: list[str] = Field(default_factory=list)
    confirmed_nodes: list[str] = Field(default_factory=list)

    @property
    def removal_objects(self) -> frozenset[str]:
        """Exact remote identities; their fences do not authorize NAS byte effects."""
        return frozenset(
            item.object.sha256
            for target in self.effects.targets
            for item in target.objects
            if item.disposition == FleetRemovalDisposition.REMOVE
        )
