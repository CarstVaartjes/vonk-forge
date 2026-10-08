"""Resource fit."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import (
    TYPE_CHECKING,
)
from typing import cast as typing_cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    ReservationState,
    RunSwitchCode,
)
from vonk_forge_contracts import ModelDefinition

from ..attempt_residues import unowned_never_installed
from ..disk_reservations import (
    describe_disk_charges,
    models_stored_on_node,
    outstanding_disk_charges,
)
from ..install_admission import (
    AGENT_UPGRADE_REQUIRED_DETAIL,
    IMAGE_PULL_CAPABILITY,
)
from ..lifecycle.evidence import (
    BookkeepingReason,
    retire_as_unknown,
)
from ..memory_reservations import (
    memory_reservations,
)
from ..models import (
    AgentNode,
    CatalogDocumentRevision,
    ResourceReservation,
)
from ..profile_capacity import (
    reservation_visible,
)
from ..recipe_runtime_specs import (
    recipe_topology,
)
from ..resource_planning import (
    ResourceDemand,
    installation_disk_requirement,
    memory_capacity_snapshot,
    memory_requirement,
    plan_capacity,
)
from ..run_admission import (
    allocate_service_port,
    run_port_blockers,
    run_port_demand,
)
from ..run_switch_contract import (
    FreshnessEvidence,
    MemoryUsageUncertainty,
    ResourceDemandEvidence,
    RunMemoryResidualRange,
    RunSwitchReason,
    SparkFit,
    SparkFitNode,
    SparkGroup,
)
from ..unused_storage_collection import spark_eviction_capacity
from .build_helpers import _recipe_model_digests
from .planning_helpers import (
    _as_reason,
    _is_memory_reservation_kind,
    _latest_inventory,
    _resource_evidence_digest,
    _resource_reason,
)

if TYPE_CHECKING:
    from .service import RunSwitchOperationService


class ResourceFitMixin:
    def _fit(
        self,
        session: Session,
        revision: CatalogDocumentRevision | None,
        group: SparkGroup,
        *,
        now: datetime,
        excluded_run_ids: Sequence[str],
        effective_settings: object | None = None,
        model_documents: Mapping[tuple[str, str, str], ModelDefinition] | None = None,
        revision_digest: str | None = None,
        serving: bool = True,
        image_bytes: int | None = None,
        artifact_bytes: int | None = None,
        excluded_profile_application_ids: tuple[str, ...] = (),
    ) -> tuple[
        list[FreshnessEvidence],
        SparkFit,
        list[RunSwitchReason],
        list[RunSwitchReason],
        dict[str, frozenset[str]],
    ]:
        service = typing_cast("RunSwitchOperationService", self)
        freshness: list[FreshnessEvidence] = []
        nodes: list[SparkFitNode] = []
        blockers: list[RunSwitchReason] = []
        warnings: list[RunSwitchReason] = []
        insufficient_components_by_node: dict[str, frozenset[str]] = {}
        adoptable = (
            unowned_never_installed(
                session,
                recipe_revision_id=revision.id,
                node_ids=frozenset(node.node_id for node in group.nodes),
            )
            if revision is not None
            else ()
        )
        role_by_name = (
            {role.name: role for role in recipe_topology(revision.document).roles}
            if revision is not None
            else {}
        )
        recipe_models = _recipe_model_digests(revision)
        excluded = set(excluded_run_ids)
        for item in group.nodes:
            snapshot, evidence = _latest_inventory(
                session,
                item.node_id,
                now=now,
                maximum_age_seconds=service._inventory_max_age,
            )
            freshness.append(evidence)
            node_blockers: list[RunSwitchReason] = []
            node_warnings: list[RunSwitchReason] = []
            ports_required: list[int] = []
            if serving and revision is not None:
                try:
                    port_demand = allocate_service_port(
                        session,
                        item.node_id,
                        run_port_demand(
                            revision.document,
                            node_count=len(group.nodes),
                            endpoint_owner=item.endpoint_owner,
                        ),
                        excluded_run_ids=excluded_run_ids,
                        excluded_profile_application_ids=excluded_profile_application_ids,
                    )
                except (TypeError, ValueError) as error:
                    node_blockers.append(
                        _as_reason(
                            RunSwitchCode.INTERFACE_INVALID,
                            str(error),
                            scope="recipe",
                            node_ids=(item.node_id,),
                        )
                    )
                else:
                    ports_required = list(port_demand.required_ports)
                    node_blockers.extend(
                        _as_reason(
                            reason.code,
                            reason.detail,
                            scope="node",
                            node_ids=(item.node_id,),
                        )
                        for reason in run_port_blockers(
                            session,
                            item.node_id,
                            port_demand,
                            excluded_run_ids=excluded_run_ids,
                            excluded_profile_application_ids=excluded_profile_application_ids,
                        )
                    )
            if snapshot is None:
                node_blockers.append(
                    _as_reason(
                        RunSwitchCode.INVENTORY_UNKNOWN,
                        "No authenticated Spark inventory is available.",
                        scope="freshness",
                        node_ids=(item.node_id,),
                    )
                )
            elif evidence.state == "stale":
                node_blockers.append(
                    _as_reason(
                        RunSwitchCode.INVENTORY_STALE,
                        "Spark inventory is older than the Run/Switch freshness policy.",
                        scope="freshness",
                        node_ids=(item.node_id,),
                        stale=True,
                    )
                )
            agent = session.get(AgentNode, item.node_id)
            if agent is None or agent.state != "active" or agent.revoked_at is not None:
                node_blockers.append(
                    _as_reason(
                        RunSwitchCode.SPARK_UNAVAILABLE,
                        "Selected Spark is not active in Controller authority.",
                        scope="node",
                        node_ids=(item.node_id,),
                    )
                )
            if (
                serving
                and snapshot is not None
                and IMAGE_PULL_CAPABILITY not in snapshot.capabilities
            ):
                node_blockers.append(
                    _as_reason(
                        RunSwitchCode.AGENT_UPGRADE_REQUIRED,
                        AGENT_UPGRADE_REQUIRED_DETAIL,
                        scope="node",
                        node_ids=(item.node_id,),
                    )
                )
            role = role_by_name.get(item.role)
            memory = None if role is None else role.resources.memory
            disk = None if role is None else role.resources.disk
            required_memory: int | None = None
            memory_kind = None
            memory_floor = None
            memory_capacity: int | None = None
            memory_available: int | None = None
            memory_free_after: int | None = None
            memory_usage_uncertainty: MemoryUsageUncertainty | None = None
            required_disk: int | None = None
            disk_free: int | None = None
            disk_free_after: int | None = None
            demand: ResourceDemand | None = None
            if memory is None or disk is None:
                node_blockers.append(
                    _as_reason(
                        RunSwitchCode.RESOURCE_CONTRACT_INVALID,
                        "The selected recipe role does not contain an exact disk and memory envelope.",
                        scope="recipe",
                        node_ids=(item.node_id,),
                    )
                )
            else:
                if not serving:
                    required_memory = 0
                else:
                    try:
                        memory_need = memory_requirement(
                            revision.document if revision is not None else {},
                            memory,
                            item.role,
                            model_documents,
                            settings=effective_settings,
                            platform_floor_bytes=service._memory_floor,
                        )
                    except (TypeError, ValueError) as error:
                        node_blockers.append(
                            _as_reason(
                                RunSwitchCode.MEMORY_ENVELOPE_INVALID,
                                str(error),
                                scope="recipe",
                                node_ids=(item.node_id,),
                            )
                        )
                    else:
                        demand = memory_need.demand
                        memory_kind = memory_need.kind
                        memory_floor = memory_need.floor_bytes
                        required_memory = demand.total_bytes
                        reservations = memory_reservations(
                            session,
                            item.node_id,
                            memory_pool=snapshot.memory_pool if snapshot else None,
                            excluded_profile_application_ids=excluded_profile_application_ids,
                            excluded_run_ids=excluded_run_ids,
                            observed_at=snapshot.observed_at if snapshot else None,
                        )
                        capacity = memory_capacity_snapshot(
                            item.node_id,
                            memory_need.kind,
                            host=(
                                snapshot.host_memory_total_bytes,
                                snapshot.host_memory_free_bytes,
                            )
                            if snapshot
                            else None,
                            accelerator=(
                                snapshot.gpu_memory_total_bytes,
                                snapshot.gpu_memory_free_bytes,
                            )
                            if snapshot
                            else None,
                            reservations=reservations,
                            memory_pool=snapshot.memory_pool if snapshot else None,
                            evidence_state="fresh"
                            if evidence.state == "fresh"
                            else "unknown",
                            evidence_digest=snapshot.evidence_digest
                            if snapshot
                            else None,
                            evidence_observed_at=snapshot.observed_at
                            if snapshot
                            else None,
                        )
                        capacity_totals = [
                            part.available_bytes for part in capacity.components
                        ]
                        known_capacity_totals = [
                            value for value in capacity_totals if type(value) is int
                        ]
                        memory_capacity = (
                            min(known_capacity_totals)
                            if capacity_totals
                            and len(known_capacity_totals) == len(capacity_totals)
                            else capacity.available_bytes
                        )
                        if (
                            capacity.available_bytes is not None
                            and capacity.occupied_bytes is not None
                        ):
                            memory_available = (
                                capacity.available_bytes - capacity.occupied_bytes
                            )
                        fit_capacity = plan_capacity(
                            {item.node_id: demand},
                            [capacity],
                            memory_floor_bytes=memory_need.floor_bytes,
                        )
                        fit_node = fit_capacity.nodes[0]
                        memory_free_after = fit_node.selected_free_after_bytes
                        if fit_node.unknown_run_residuals:
                            if (
                                snapshot is not None
                                and evidence.observed_at is not None
                                and evidence.evidence_digest is not None
                            ):
                                residual_ranges = []
                                for residual in sorted(
                                    fit_node.unknown_run_residuals,
                                    key=lambda value: (
                                        value.run_id,
                                        value.run_generation,
                                        value.reservation_kind,
                                    ),
                                ):
                                    if not _is_memory_reservation_kind(
                                        residual.reservation_kind
                                    ):
                                        # A claim of a kind this contract cannot
                                        # name is left out of the typed range and
                                        # recorded; the fit above already counted
                                        # it, so the plan stays conservative.
                                        retire_as_unknown(
                                            "run-switch.memory-claim",
                                            residual.run_id,
                                            BookkeepingReason.PERSISTED_STATE_DAMAGED,
                                            "an active run memory claim has an "
                                            "invalid kind",
                                        )
                                        continue
                                    residual_ranges.append(
                                        RunMemoryResidualRange(
                                            run_id=residual.run_id,
                                            run_generation=residual.run_generation,
                                            reservation_kind=residual.reservation_kind,
                                            maximum_bytes=residual.maximum_bytes,
                                        )
                                    )
                                memory_usage_uncertainty = MemoryUsageUncertainty(
                                    source="aggregate_inventory_without_run_usage",
                                    inventory_observed_at=evidence.observed_at,
                                    inventory_evidence_digest=evidence.evidence_digest,
                                    residual_ranges=residual_ranges,
                                )
                            # An exact numeric headroom would imply measured
                            # per-run usage. The typed range and fit decision
                            # carry the conservative bound instead.
                            memory_free_after = None
                        if fit_node.insufficient_components:
                            insufficient_components_by_node[item.node_id] = frozenset(
                                "shared"
                                if snapshot is not None
                                and snapshot.memory_pool == "shared"
                                else component
                                for component in fit_node.insufficient_components
                            )
                        for reason in fit_node.reasons:
                            projected = _resource_reason(
                                reason, node_ids=(item.node_id,)
                            )
                            if projected.severity == "warning":
                                node_warnings.append(projected)
                            else:
                                node_blockers.append(projected)
                disk_need = None
                try:
                    image_size = (
                        image_bytes if image_bytes is not None else disk.image_bytes
                    )
                    artifact_size = (
                        artifact_bytes
                        if artifact_bytes is not None
                        else disk.artifact_bytes
                    )
                    # An unstated payload size leaves the envelope incomplete.
                    if image_size is not None and artifact_size is not None:
                        if recipe_models and recipe_models <= models_stored_on_node(
                            session, item.node_id
                        ):
                            # The Spark's shared store already holds every model of
                            # the recipe (an installation of it exists there), so
                            # the install links those files and writes none.
                            artifact_size = 0
                        disk_need = installation_disk_requirement(
                            disk,
                            required_download_bytes=image_size + artifact_size,
                            minimum_floor_bytes=(
                                service._lifecycle._install_admission._disk_floor
                                if service._lifecycle is not None
                                else 0
                            ),
                        )
                except (TypeError, ValueError):
                    disk_need = None
                if disk_need is None:
                    node_blockers.append(
                        _as_reason(
                            RunSwitchCode.DISK_ENVELOPE_INVALID,
                            "The recipe disk envelope is incomplete.",
                            scope="recipe",
                            node_ids=(item.node_id,),
                        )
                    )
                else:
                    # Review promises a full allocation, including headroom,
                    # less the model files the Spark's store already holds.
                    # Installation may reduce it for exact target-local reuse;
                    # it must never grow a claim after operator acceptance.
                    required_disk = disk_need.required_bytes + disk_need.floor_bytes
                    disk_free = (
                        snapshot.disk_free_bytes if snapshot is not None else None
                    )
                    if snapshot is not None and disk_free is not None:
                        charges = outstanding_disk_charges(
                            session,
                            item.node_id,
                            inventory_observed_at=snapshot.observed_at,
                            excluded_run_ids=excluded,
                            excluded_profile_application_ids=excluded_profile_application_ids,
                            # This attempt adopts the unowned plan a failed
                            # attempt of the same recipe left on these Sparks,
                            # so its claim is not capacity to wait for.
                            excluded_installation_ids=adoptable,
                        )
                        reserved_disk = sum(charge.amount_bytes for charge in charges)
                        disk_free_after = disk_free - reserved_disk - required_disk
                        if disk_free_after < 0:
                            holders = describe_disk_charges(session, charges)
                            # Cleanup is space-driven: unused installations the
                            # collector may remove cover a shortfall, so the
                            # review plans that eviction (the load waits for it)
                            # and refuses only for what nothing can free.
                            capacity = spark_eviction_capacity(
                                session, item.node_id, now
                            )
                            evictable, kept_sentence = (
                                capacity.freeable,
                                capacity.kept,
                            )
                            if evictable >= -disk_free_after:
                                node_warnings.append(
                                    _as_reason(
                                        RunSwitchCode.DISK_EVICTION_PLANNED,
                                        f"The operation needs {required_disk} bytes and "
                                        f"{-disk_free_after} more must be freed on "
                                        "this Spark: "
                                        + capacity.plan(-disk_free_after)
                                        + ", least recently used first. Models stay "
                                        "on the NAS, so a later load reinstalls.",
                                        scope="node",
                                        node_ids=(item.node_id,),
                                        severity="warning",
                                    )
                                )
                            else:
                                node_blockers.append(
                                    _as_reason(
                                        RunSwitchCode.INSUFFICIENT_DISK,
                                        f"The operation needs {required_disk} bytes and would leave {disk_free_after} bytes."
                                        + (f" Disk is {holders}." if holders else "")
                                        + f" Only {evictable} bytes of unused installations can be removed. "
                                        + kept_sentence,
                                        scope="node",
                                        node_ids=(item.node_id,),
                                    )
                                )
            nodes.append(
                SparkFitNode(
                    node_id=item.node_id,
                    rank=item.rank,
                    role=item.role,
                    allowed=not node_blockers,
                    ports_required=ports_required,
                    disk_required_bytes=required_disk,
                    disk_free_bytes=disk_free,
                    disk_free_after_bytes=disk_free_after,
                    memory_required_bytes=required_memory,
                    memory_kind=memory_kind,
                    memory_pool=snapshot.memory_pool if snapshot else None,
                    memory_floor_bytes=memory_floor,
                    memory_capacity_bytes=memory_capacity,
                    memory_available_bytes=memory_available,
                    memory_free_after_bytes=memory_free_after,
                    memory_usage_uncertainty=memory_usage_uncertainty,
                    resource_demand=(
                        ResourceDemandEvidence(
                            weights_bytes=demand.weights_bytes,
                            runtime_overhead_bytes=demand.runtime_overhead_bytes,
                            context_bytes=demand.context_bytes,
                            concurrency_bytes=demand.concurrency_bytes,
                            batch_bytes=demand.batch_bytes,
                            total_bytes=demand.total_bytes,
                            evidence_state=demand.evidence_state,
                            evidence_digest=_resource_evidence_digest(
                                revision_digest,
                            ),
                        )
                        if demand is not None
                        else None
                    ),
                    blockers=node_blockers,
                    warnings=node_warnings,
                )
            )
            blockers.extend(node_blockers)
            warnings.extend(node_warnings)
        return (
            freshness,
            SparkFit(
                allowed=not blockers,
                nodes=nodes,
                blockers=blockers,
                warnings=warnings,
            ),
            blockers,
            warnings,
            insufficient_components_by_node,
        )

    def _active_reservation_bytes(
        self,
        session: Session,
        node_id: str,
        kind: str | None,
        excluded_run_ids: set[str],
        *,
        excluded_profile_application_ids: tuple[str, ...] = (),
    ) -> int:
        if kind is None:
            return 0
        reservations = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.node_id == node_id,
                    ResourceReservation.kind == kind,
                    ResourceReservation.state == ReservationState.ACTIVE,
                    reservation_visible(excluded_profile_application_ids),
                )
            )
        )
        total = 0
        for reservation in reservations:
            if (
                reservation.owner_kind == "run"
                and reservation.owner_id in excluded_run_ids
            ):
                continue
            total += reservation.amount_bytes
        return total
