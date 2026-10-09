"""Run switch contract: requests."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    Field,
    StringConstraints,
    model_validator,
)

from ..integer_domains import MAX_DATABASE_INTEGER
from ..run_switch_identity_contract import NodeId, UuidId
from ..strict_json import StrictModel
from .vocabulary import Alias, Digest, RunSwitchPlacementAction, RunSwitchRetention


class InvocationMetadata(StrictModel):
    """Context for audit and tracing which has no decision-making authority."""

    origin: Annotated[
        str,
        StringConstraints(
            min_length=1,
            max_length=64,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$",
        ),
    ] = "operator"
    correlation_id: UuidId | None = None
    reason: Annotated[str, StringConstraints(max_length=256)] | None = None
    context: dict[
        Annotated[str, StringConstraints(min_length=1, max_length=64)],
        Annotated[str, StringConstraints(max_length=256)],
    ] = Field(default_factory=dict, max_length=16)


class SparkGroupNode(StrictModel):
    node_id: NodeId
    rank: int = Field(ge=0, le=31)
    role: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    endpoint_owner: bool = False


class SparkGroup(StrictModel):
    """A complete, rank-labelled Spark group selected by the operator."""

    nodes: list[SparkGroupNode] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_group(self) -> SparkGroup:
        node_ids = [node.node_id for node in self.nodes]
        ranks = [node.rank for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("Spark group node IDs must be unique")
        if sorted(ranks) != list(range(len(ranks))):
            raise ValueError("Spark group ranks must be contiguous from zero")
        if sum(node.endpoint_owner for node in self.nodes) != 1:
            raise ValueError("Spark group must have exactly one endpoint owner")
        return self


class RunSwitchProfileStopScope(StrictModel):
    """Reviewed profile-only cleanup of the reachable ranks in a lost group.

    The full accepted topology remains visible even though only its reachable
    subset is sent Stop work.  Missing ranks are explicit so a partial cleanup
    can never be presented as a successful full-group stop.
    """

    original_group: SparkGroup
    target_node_ids: list[NodeId] = Field(min_length=1, max_length=32)
    missing_node_ids: list[NodeId] = Field(min_length=1, max_length=31)

    @model_validator(mode="after")
    def exact_partition(self) -> RunSwitchProfileStopScope:
        original = [node.node_id for node in self.original_group.nodes]
        targets = self.target_node_ids
        missing = self.missing_node_ids
        if targets != sorted(set(targets)) or missing != sorted(set(missing)):
            raise ValueError("profile Stop scope node IDs must be sorted and unique")
        if len(original) < 2:
            raise ValueError("profile partial Stop requires a multi-Spark group")
        if set(targets) & set(missing) or set(targets) | set(missing) != set(original):
            raise ValueError(
                "profile Stop targets and missing ranks must partition the group"
            )
        return self


class RunMemoryResidualRange(StrictModel):
    """Possible remaining bytes for one exact active run reservation."""

    run_id: UuidId
    run_generation: int = Field(ge=1, le=2**63 - 1)
    reservation_kind: Literal["host-memory", "gpu-memory", "unified-memory"]
    minimum_bytes: Literal[0] = 0
    maximum_bytes: int = Field(ge=0)


class MemoryUsageUncertainty(StrictModel):
    """Fresh aggregate capacity lacks per-run resident usage evidence."""

    source: Literal["aggregate_inventory_without_run_usage"]
    inventory_observed_at: datetime
    inventory_evidence_digest: Digest
    residual_ranges: list[RunMemoryResidualRange] = Field(max_length=128)

    @model_validator(mode="after")
    def unique_ordered_claims(self) -> MemoryUsageUncertainty:
        keys = [
            (item.run_id, item.run_generation, item.reservation_kind)
            for item in self.residual_ranges
        ]
        if keys != sorted(set(keys)):
            raise ValueError("memory uncertainty claims must be unique and ordered")
        return self


class RunSwitchPreviewRequest(StrictModel):
    schema_version: Literal[2] = 2
    model_content_sha256: Digest
    recipe_revision_id: UuidId
    spark_group: SparkGroup
    alias: Alias
    action: RunSwitchPlacementAction = "run"
    retention: RunSwitchRetention = "retain-cached"
    # Recipe option name -> chosen value. An option left out takes the
    # recipe's default; an unknown name or value is refused.
    option_choices: dict[str, str] = Field(default_factory=dict, max_length=16)
    invocation: InvocationMetadata = Field(default_factory=InvocationMetadata)


class RunSwitchApplyRequest(RunSwitchPreviewRequest):
    # Preview binding and idempotency are generated by the Controller when a
    # caller uses the one-step Run/Switch route.  They remain accepted for
    # advanced clients that explicitly replay a reviewed plan.
    plan_digest: Digest | None = None
    request_key: UuidId | None = None


class RunSwitchStopPreviewRequest(StrictModel):
    schema_version: Literal[2] = 2
    run_id: UuidId
    invocation: InvocationMetadata = Field(default_factory=InvocationMetadata)


class RunSwitchStopApplyRequest(RunSwitchStopPreviewRequest):
    plan_digest: Digest | None = None
    request_key: UuidId | None = None


class RunSwitchCleanupPreviewRequest(StrictModel):
    """Ask Run/Switch to remove one installation that is no longer desired.

    Cleanup is authorized by the installation's own uninstall assessment, so it
    never requires launch readiness: removing work must not depend on being able
    to start work.  Run/Switch still owns the sequencing, the child reference
    and the retry budget for the removal.
    """

    installation_id: UuidId
    cleanup_mode: Literal["uninstall", "reconcile"] = "uninstall"


class RunSwitchCleanupApplyRequest(RunSwitchCleanupPreviewRequest):
    plan_digest: Digest | None = None
    request_key: UuidId | None = None


class InstallationReconcileRequest(StrictModel):
    """Reconcile the reviewed plan, or use the Controller's one-step decision."""

    request_key: UuidId
    plan_digest: Digest | None = None


class RunSwitchReconciliationTarget(StrictModel):
    """One exact rank and whether its cleanup already succeeded."""

    node_id: NodeId
    rank: int = Field(ge=0, le=31)
    role: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    installed_bytes: int = Field(ge=0)
    state: Literal["pending", "reconciled"]


class RunSwitchReconciliationAuthority(StrictModel):
    """Controller-owned identity of the installation a repair removes.

    The accepted installation plan remains opaque; it never claims that
    malformed launch metadata is executable.
    """

    schema_version: Literal[2] = 2
    installation_id: UuidId
    original_plan_digest: Digest
    recipe_revision_id: UuidId
    recipe_content_sha256: Digest
    mapping_id: UuidId
    mapping_generation: int = Field(le=MAX_DATABASE_INTEGER, ge=1)
    recipe_build_id: UuidId | None
    image_digest: Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
    model_content_sha256: Digest | None
    targets: list[RunSwitchReconciliationTarget] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def targets_are_exact_and_ordered(self) -> RunSwitchReconciliationAuthority:
        keys = [(target.rank, target.node_id) for target in self.targets]
        node_ids = [target.node_id for target in self.targets]
        ranks = [target.rank for target in self.targets]
        if (
            len(set(node_ids)) != len(node_ids)
            or len(set(ranks)) != len(ranks)
            or sorted(ranks) != list(range(len(self.targets)))
            or keys != sorted(keys)
        ):
            raise ValueError(
                "reconciliation targets must have unique nodes and contiguous ranks"
            )
        return self
