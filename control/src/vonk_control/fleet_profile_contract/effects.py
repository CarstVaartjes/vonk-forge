"""Fleet profile contract: effects."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import (
    Field,
    model_validator,
)
from vonk_agent_protocol import (
    OperationProgress,
)

from ..integer_domains import MAX_DATABASE_INTEGER
from ..run_switch_contract import (
    RunSwitchProfileStopScope,
)
from ..strict_json import StrictModel
from .vocabulary import Alias, Digest, FleetProfileChildPhase, NodeId, UuidId


class FleetProfileRunEffect(StrictModel):
    run_id: UuidId
    installation_id: UuidId
    alias: Alias
    # node_ids always names the full stored topology.  For a profile-owned
    # cleanup after fleet removal, this separately binds the live Stop targets
    # and missing ranks; ordinary Stops keep this field absent.
    node_ids: list[NodeId] = Field(min_length=1, max_length=32)
    action: Literal["keep", "stop"]
    profile_stop_scope: RunSwitchProfileStopScope | None = None

    @model_validator(mode="after")
    def partial_scope_matches_effect(self) -> FleetProfileRunEffect:
        if self.node_ids != sorted(set(self.node_ids)):
            raise ValueError("profile run effect node IDs must be sorted and unique")
        scope = self.profile_stop_scope
        if scope is not None and (
            self.action != "stop"
            or self.node_ids
            != sorted(node.node_id for node in scope.original_group.nodes)
        ):
            raise ValueError("profile Stop scope differs from its full run effect")
        return self


class FleetProfileInstallationEffect(StrictModel):
    installation_id: UuidId
    node_ids: list[NodeId] = Field(min_length=1, max_length=32)
    action: Literal["keep", "remove"]


class FleetProfilePendingEffect(StrictModel):
    kind: Literal["job", "profile-application"]
    id: UuidId
    node_ids: list[NodeId] = Field(min_length=1, max_length=32)


class FleetProfileAdoptedStopEffect(StrictModel):
    """Exact original cleanup, retained by a newer whole-fleet decision."""

    effect: FleetProfileRunEffect
    queue_index: int = Field(ge=0)
    operation_id: UuidId
    request_key: UuidId

    @model_validator(mode="after")
    def is_stop(self) -> FleetProfileAdoptedStopEffect:
        if self.effect.action != "stop":
            raise ValueError("adopted cleanup must be an exact Stop effect")
        return self


class FleetProfileAdoptedApplicationEffect(StrictModel):
    """An exact continuing executor authorized by the newer reviewed snapshot."""

    application_id: UuidId
    plan_digest: Digest
    workload_intent_ordinal: int = Field(le=MAX_DATABASE_INTEGER, ge=1)
    node_ids: list[NodeId] = Field(min_length=1, max_length=32)
    assignment_ids: list[UuidId] = Field(default_factory=list, max_length=64)
    # No cleanup adoption has the same canonical wire as before this optional
    # effect was introduced; accepted assignment-only review digests stay exact.
    stops: list[FleetProfileAdoptedStopEffect] = Field(
        default_factory=list, exclude_if=lambda value: not value
    )

    @model_validator(mode="after")
    def scope_is_canonical(self) -> FleetProfileAdoptedApplicationEffect:
        if not self.assignment_ids and not self.stops:
            raise ValueError("adoption requires an assignment or exact cleanup")
        if len({stop.effect.run_id for stop in self.stops}) != len(self.stops):
            raise ValueError("adopted cleanup identities must be unique")
        if any(
            not set(stop.effect.node_ids) <= set(self.node_ids) for stop in self.stops
        ):
            raise ValueError("adopted cleanup must retain its complete topology")
        if self.node_ids != sorted(set(self.node_ids)):
            raise ValueError("adopted effect nodes must be sorted and unique")
        if self.assignment_ids != sorted(set(self.assignment_ids)):
            raise ValueError("adopted assignment IDs must be sorted and unique")
        return self


class FleetProfileEffects(StrictModel):
    """Identified live effects, including complete distributed membership."""

    runs: list[FleetProfileRunEffect]
    installations: list[FleetProfileInstallationEffect]
    superseded: list[FleetProfilePendingEffect]
    adopted: list[FleetProfileAdoptedApplicationEffect] = Field(
        default_factory=list, max_length=64
    )


class FleetProfileChildProgress(StrictModel):
    """Typed progress emitted by the profile-owned Run switch adapter."""

    operation: OperationProgress | None = None
    startup_budget_seconds: int | None = Field(default=None, ge=1)
    start_deadline: datetime | None = None

    phase: FleetProfileChildPhase
    node_ids: list[NodeId] = Field(default_factory=list, max_length=32)
    bytes: int | None = Field(default=None, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def byte_progress_is_consistent(self) -> FleetProfileChildProgress:
        if self.node_ids != sorted(self.node_ids) or len(self.node_ids) != len(
            set(self.node_ids)
        ):
            raise ValueError("child progress node IDs must be sorted and unique")
        if (
            self.bytes is not None
            and self.total_bytes is not None
            and self.bytes > self.total_bytes
        ):
            raise ValueError("child progress bytes cannot exceed total bytes")
        return self
