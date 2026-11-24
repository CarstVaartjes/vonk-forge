"""Stored stop-authority documents a Fleet profile stop carries."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, model_validator

from vonk_agent_protocol.identifiers import NodeId
from vonk_agent_protocol.integer_domains import (
    MAX_DATABASE_BIGINT,
    MAX_DATABASE_INTEGER,
)
from vonk_agent_protocol.lifecycle_vocabulary import InvalidRequestReason
from vonk_agent_protocol.outcome import InvalidRequestError
from vonk_agent_protocol.reason_codes.profiles import ProjectionCode
from vonk_agent_protocol.recipe_operations import RecipeStopPayload
from vonk_agent_protocol.wire_model import canonical_message
from vonk_control.categorized_errors import BookkeepingUnknown
from vonk_control.strict_json import StrictJSONModel

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


_NODE_ID = re.compile(r"^spk_[0-9a-f]{32}$")


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class OfflineStopIntent(StrictJSONModel):
    """Exact Stop orders retained for reconciliation after node contact returns."""

    code: Literal[ProjectionCode.NODE_OFFLINE] = ProjectionCode.NODE_OFFLINE
    node_ids: list[NodeId] = Field(min_length=1, max_length=32)


class ProfileJobRunStopTarget(StrictJSONModel):
    """One exact transient runtime and its durable JobRun source."""

    artifact_job_id: str = Field(min_length=36, max_length=36, pattern=_UUID.pattern)
    source_job_id: str = Field(min_length=36, max_length=36, pattern=_UUID.pattern)
    source_operation_id: str = Field(
        min_length=36, max_length=36, pattern=_UUID.pattern
    )
    node_id: str = Field(min_length=36, max_length=36, pattern=_NODE_ID.pattern)
    stop_payload_sha256: str = Field(
        min_length=64, max_length=64, pattern=_SHA256.pattern
    )

    @property
    def key(self) -> tuple[str, str]:
        """The Stop's identity: its Spark and the job container it removes."""
        return self.node_id, self.artifact_job_id


def jobrun_stop_key(node_id: str, stop: RecipeStopPayload) -> tuple[str, str]:
    """A JobRun Stop order's identity, matched against :attr:`ProfileJobRunStopTarget.key`.

    The order is its target's by the Spark it runs on and the container it
    names, never by a digest of its document: a document read back under a
    newer Controller hashes differently and still names the same container.
    """
    return node_id, stop.target_runtime_id


class RunStopScope(StrictJSONModel):
    """An exact run and complete membership under its accepted Stop intent."""

    schema_version: Literal[1]
    run_id: str = Field(min_length=36, max_length=36, pattern=_UUID.pattern)
    installation_id: str = Field(min_length=36, max_length=36, pattern=_UUID.pattern)
    recipe_revision_id: str = Field(min_length=36, max_length=36, pattern=_UUID.pattern)
    mapping_id: str = Field(min_length=36, max_length=36, pattern=_UUID.pattern)
    mapping_generation: int = Field(le=MAX_DATABASE_INTEGER, ge=1)
    run_generation: int = Field(le=MAX_DATABASE_BIGINT, ge=1)
    plan_digest: str = Field(min_length=64, max_length=64, pattern=_SHA256.pattern)
    workload_intent_ordinal: int = Field(le=MAX_DATABASE_INTEGER, ge=1)
    run_node_ids: list[str] = Field(min_length=1, max_length=32)
    reachable_node_ids: list[str] = Field(min_length=1, max_length=32)
    missing_node_ids: list[str] = Field(default_factory=list, max_length=31)
    stop_plan_digest: str = Field(min_length=64, max_length=64, pattern=_SHA256.pattern)

    @model_validator(mode="after")
    def membership_is_exact(self) -> RunStopScope:
        nodes, reachable, missing = (
            self.run_node_ids,
            self.reachable_node_ids,
            self.missing_node_ids,
        )
        if (
            nodes != list(dict.fromkeys(nodes))
            or reachable != sorted(set(reachable))
            or missing != sorted(set(missing))
            or set(reachable) & set(missing)
            or set(reachable) | set(missing) != set(nodes)
            or (bool(missing) and len(nodes) < 2)
        ):
            raise BookkeepingUnknown("Stop run membership is ambiguous")
        return self


class JobRunStopScope(RunStopScope):
    """The exact issued transient targets within an accepted run Stop."""

    targets: list[ProfileJobRunStopTarget] = Field(default_factory=list)
    unissued_artifact_job_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def targets_are_exact(self) -> JobRunStopScope:
        # A job on several Sparks has one target per issued rank: its Spark and
        # container name the Stop (``key``), and each rank is its own order.
        keys = [target.key for target in self.targets]
        source_operations = [target.source_operation_id for target in self.targets]
        if (
            len(keys) != len(set(keys))
            or len(source_operations) != len(set(source_operations))
            or {target.artifact_job_id for target in self.targets}
            & set(self.unissued_artifact_job_ids)
            or any(
                target.node_id not in self.reachable_node_ids for target in self.targets
            )
        ):
            raise BookkeepingUnknown("profile JobRun Stop authorization is ambiguous")
        return self


class ProfileStopOwnerBinding(RunStopScope):
    """The canonical accepted profile operation that owns this exact Stop."""

    profile_application_id: str = Field(
        min_length=36, max_length=36, pattern=_UUID.pattern
    )
    profile_operation_id: str = Field(
        min_length=36, max_length=36, pattern=_UUID.pattern
    )
    profile_digest: str = Field(min_length=64, max_length=64, pattern=_SHA256.pattern)
    profile_plan_digest: str = Field(
        min_length=64, max_length=64, pattern=_SHA256.pattern
    )
    profile_step: int = Field(ge=0)


class ProfileJobRunStopAuthorization(ProfileStopOwnerBinding, JobRunStopScope):
    """Current accepted profile Stop owns this immutable JobRun scope."""


class ProfileJobRunStopPhaseItem(StrictJSONModel):
    operation_id: str = Field(min_length=36, max_length=36, pattern=_UUID.pattern)
    node_id: str = Field(min_length=36, max_length=36, pattern=_NODE_ID.pattern)
    payload: RecipeStopPayload


class ProfileJobRunStopJob(StrictJSONModel):
    """The one-shot Stop parent persisted for a current profile owner."""

    schema_version: Literal[1]
    owner_kind: Literal["run"]
    owner_id: str = Field(min_length=36, max_length=36, pattern=_UUID.pattern)
    plan_digest: str = Field(min_length=64, max_length=64, pattern=_SHA256.pattern)
    workload_intent_ordinal: int = Field(le=MAX_DATABASE_INTEGER, ge=1)
    offline_stop_intent: OfflineStopIntent | None = None
    execution_mode: Literal["profile-jobrun-stop"]
    profile_application_id: str = Field(
        min_length=36, max_length=36, pattern=_UUID.pattern
    )
    profile_operation_id: str = Field(
        min_length=36, max_length=36, pattern=_UUID.pattern
    )
    profile_stop_authorization: ProfileJobRunStopAuthorization
    phases: list[list[ProfileJobRunStopPhaseItem]] = Field(min_length=1)

    @property
    def flattened_phase_items(self) -> tuple[ProfileJobRunStopPhaseItem, ...]:
        return tuple(item for phase in self.phases for item in phase)

    @classmethod
    def model_validate_parent(cls, raw: object) -> ProfileJobRunStopJob:
        """Validate a stored parent document; the strict model is the typed shape."""
        parent = cls.model_validate_json(canonical_message(raw), strict=True)
        authorization = parent.profile_stop_authorization
        if (
            parent.owner_id != authorization.run_id
            or parent.profile_application_id != authorization.profile_application_id
            or parent.profile_operation_id != authorization.profile_operation_id
            or parent.workload_intent_ordinal != authorization.workload_intent_ordinal
            or parent.plan_digest != authorization.stop_plan_digest
            or any(not phase for phase in parent.phases)
            or any(
                len({item.node_id for item in phase}) != len(phase)
                for phase in parent.phases
            )
        ):
            raise BookkeepingUnknown(
                "profile JobRun Stop parent identity is inconsistent"
            )
        target_keys = {item.key for item in authorization.targets}
        child_keys = [
            jobrun_stop_key(item.node_id, item.payload)
            for item in parent.flattened_phase_items
        ]
        if (
            len(target_keys) != len(authorization.targets)
            or len(child_keys) != len(set(child_keys))
            or set(child_keys) != target_keys
            or any(
                item.payload.cancel_pending_start is not True
                for item in parent.flattened_phase_items
            )
        ):
            raise BookkeepingUnknown("profile JobRun Stop child set is inconsistent")
        return parent


class ProfileStopAuthorityError(InvalidRequestError, ValueError):
    """The accepted profile Stop link no longer matches current state.

    This is drift between the Controller's own records (a newer intent, a moved
    step, a changed run generation), never a security refusal: the Stop is not
    issued, and the profile owner re-plans it under a fresh request
    (``SUPERSEDED``).  Stored evidence that does not parse is ``MALFORMED``; an
    owner that cannot be read right now is :class:`BookkeepingUnknown` instead.
    """

    def __init__(
        self,
        *args: object,
        reason: InvalidRequestReason = InvalidRequestReason.SUPERSEDED,
    ) -> None:
        super().__init__(*args, reason=reason)
