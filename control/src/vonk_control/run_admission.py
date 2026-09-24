"""Mapping-fenced memory, port, capability, and fabric admission."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import asdict, dataclass, replace
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol.compiled_execution_plan import MemoryKind
from vonk_agent_protocol.inventory import MemoryPool

from .admission_locking import (
    AdmissionLockBusy,
    AdmissionRowLock,
    acquire_admission_keys,
    is_admission_contention,
    lock_admission_rows,
    node_admission_key,
)
from .install_admission import AdmissionReason
from .inventory_repository import InventoryRepository
from .legal_admission import territorial_admission
from .memory_reservations import memory_reservations
from .models import (
    AgentNode,
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    InstallationNode,
    NodeInventorySnapshot,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from .profile_capacity import (
    inherited_profile_memory,
    inherited_profile_ports,
    reservation_visible,
)
from .recipe_execution_contract import (
    RecipeExecutionContractError,
    run_plan_document,
)
from .recipe_runtime_specs import (
    RecipeRuntimeSpecError,
    recipe_topology,
    resolve_recipe_entities,
)
from .resource_planning import (
    memory_capacity_snapshot,
    memory_requirement,
    memory_reservation_kind,
    plan_capacity,
)
from .topology import Placement, TopologyError, validate_topology

_DISTRIBUTED_START_CAPABILITY = "recipe.start.two-phase.v1"
_EXACT_RUN_INSPECTION_CAPABILITY = "recipe.run.inspect.exact.v1"
_SIGNED_RUN_INSPECTION_CAPABILITY = "recipe.run.inspect.receipt.v1"

_PORT_CONFLICTS = {
    "service": (
        "run.port_occupied",
        "Port {port} is already reserved on this GPU node.",
    ),
    "rendezvous": (
        "run.rendezvous_port_occupied",
        "Multi-node rendezvous port {port} is already reserved.",
    ),
}
PORT_ADMISSION_CODES = frozenset(code for code, _ in _PORT_CONFLICTS.values())


@dataclass(frozen=True, slots=True)
class RunPortDemand:
    """The ports one mapped rank requires, including before installation."""

    service_port: int | None
    rendezvous_port: int | None

    @property
    def logical_job(self) -> bool:
        return self.service_port is None

    @property
    def plan_port(self) -> int:
        # The current run receipt has an integer port even for a logical job.
        # That placeholder is never admitted or reserved as a network port.
        return 1024 if self.service_port is None else self.service_port

    @property
    def required_ports(self) -> tuple[int, ...]:
        return tuple(
            sorted(
                {
                    port
                    for port in (self.service_port, self.rendezvous_port)
                    if port is not None
                }
            )
        )


def run_port_demand(
    document: Mapping[str, object], *, node_count: int, endpoint_owner: bool
) -> RunPortDemand:
    interfaces = document.get("interfaces")
    if not isinstance(interfaces, list):
        raise TypeError("recipe interfaces are invalid")
    interface = next(
        (
            item
            for item in interfaces
            if isinstance(item, Mapping) and item.get("adapter") == "openai"
        ),
        None,
    )
    artifact_interfaces = [
        item
        for item in interfaces
        if isinstance(item, Mapping)
        and item.get("adapter")
        in {"audio-job", "video-job", "image-job", "mesh-job", "artifact-job"}
    ]
    if interface is None and len(artifact_interfaces) == 1:
        if node_count != 1:
            raise TypeError("artifact job recipes currently require one node")
        return RunPortDemand(None, None)
    port = interface.get("port") if interface is not None else None
    if type(port) is not int or not 1 <= port <= 65535:
        raise TypeError("recipe interface port is invalid")
    return RunPortDemand(port, 29500 if node_count > 1 and endpoint_owner else None)


def run_port_blockers(
    session: Session,
    node_id: str,
    demand: RunPortDemand,
    *,
    excluded_run_ids: Sequence[str] = (),
    excluded_profile_application_ids: Sequence[str] = (),
) -> tuple[AdmissionReason, ...]:
    reservations = session.scalars(
        select(ResourceReservation).where(
            ResourceReservation.node_id == node_id,
            ResourceReservation.kind == "port",
            ResourceReservation.state.in_(("active", "promised")),
            reservation_visible(
                excluded_profile_application_ids, excluded_run_ids=excluded_run_ids
            ),
            ResourceReservation.resource_key.in_(
                tuple(str(port) for port in demand.required_ports)
            ),
        )
    )
    occupied = {item.resource_key for item in reservations}
    blockers = []
    for kind, port in (
        ("service", demand.service_port),
        ("rendezvous", demand.rendezvous_port),
    ):
        if port is None:
            continue
        code, detail = _PORT_CONFLICTS[kind]
        if kind == "rendezvous" and port == demand.service_port:
            blockers.append(
                AdmissionReason(
                    code, f"Port {port} is required by both serving and rendezvous."
                )
            )
        elif str(port) in occupied:
            blockers.append(AdmissionReason(code, detail.format(port=port)))
    return tuple(blockers)


class RunPlanConflict(RuntimeError):
    pass


class RunAdmissionBusy(RunPlanConflict):
    """A competing capacity writer requires rescheduling this same admission."""

    code = "run.capacity_busy"


def _active_recipe_revision(
    session: Session,
    revision_id: str | None,
    *,
    for_update: bool = False,
) -> CatalogDocumentRevision | None:
    """Load only an active canonical Recipe revision for run admission."""

    if not isinstance(revision_id, str) or not revision_id:
        return None
    statement = select(CatalogDocumentRevision).where(
        CatalogDocumentRevision.id == revision_id,
        CatalogDocumentRevision.kind == "recipe",
        CatalogDocumentRevision.state == "active",
    )
    if for_update:
        statement = statement.with_for_update(of=CatalogDocumentRevision)
    return session.scalar(statement)


@dataclass(frozen=True, slots=True)
class RunNodePlan:
    node_id: str
    rank: int
    role: str
    endpoint_owner: bool
    port: int
    allowed: bool
    inventory_observed_at: datetime | None
    memory_kind: MemoryKind
    required_memory_bytes: int
    available_memory_bytes: int | None
    active_reserved_bytes: int
    free_after_bytes: int | None
    memory_floor_bytes: int
    fabric_address: str | None
    fabric_bandwidth_mbps: int | None
    rendezvous_port: int | None
    blockers: tuple[AdmissionReason, ...]
    warnings: tuple[AdmissionReason, ...]
    memory_pool: MemoryPool | None


@dataclass(frozen=True, slots=True)
class RunPlan:
    installation_id: str
    alias: str
    mapping_id: str
    mapping_generation: int
    recipe_revision_id: str
    allowed: bool
    nodes: tuple[RunNodePlan, ...]
    plan_digest: str


class RunAdmissionService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        inventory_max_age: int = 300,
        memory_floor_bytes: int = 0,
    ) -> None:
        self._sessions = sessions
        self._inventory = InventoryRepository(sessions)
        self._max_age = inventory_max_age
        self._floor = memory_floor_bytes

    def plan_run(
        self,
        installation_id: str,
        alias: str,
        *,
        now: datetime,
        released_run_ids: Collection[str] = (),
        _session: Session | None = None,
        profile_application_id: str | None = None,
        excluded_profile_application_ids: Sequence[str] = (),
    ) -> RunPlan:
        """Build the admission plan for one run.

        ``released_run_ids`` names runs that a reviewed plan stops before it
        starts this one.  Their Stop phase releases their reservations, so
        counting those bytes here refuses the replacement for the workload it
        replaces -- the deadlock an operator can only break by hand.  This is a
        preview-only relaxation: accepting the run still re-derives the plan
        without it and refuses when the Stop did not really release the
        capacity (``run.plan_stale_or_blocked``).
        """
        with (
            nullcontext(_session) if _session is not None else self._sessions()
        ) as session:
            installation = session.get(RecipeInstallation, installation_id)
            if installation is None:
                raise KeyError(installation_id)
            if installation.state != "installed":
                raise ValueError("recipe installation is not complete")
            mapping = session.get(ClusterMapping, installation.mapping_id)
            if (
                mapping is None
                or mapping.state != "ready"
                or mapping.generation != installation.mapping_generation
            ):
                raise ValueError(
                    "cluster mapping generation changed after installation"
                )
            revision = _active_recipe_revision(session, installation.recipe_revision_id)
            if revision is None or revision.state != "active":
                raise ValueError("recipe revision is unavailable")
            try:
                resolved_entities = resolve_recipe_entities(session, revision.document)
            except RecipeRuntimeSpecError as error:
                raise ValueError("exact recipe dependencies are unavailable") from error
            resolved_models = resolved_entities.get("models")
            model_documents = (
                {
                    (item.publisher, item.slug, item.content_digest): item.document
                    for item in resolved_models
                    if isinstance(item, CatalogDocumentRevision)
                }
                if isinstance(resolved_models, Sequence)
                else {}
            )
            model_version = (
                resolved_models[0]
                if isinstance(resolved_models, Sequence)
                and not isinstance(resolved_models, (str, bytes))
                and resolved_models
                else None
            )
            model_document = getattr(model_version, "document", None)
            if not isinstance(model_document, Mapping):
                raise ValueError(  # noqa: TRY004
                    "exact model license authority is unavailable"
                )
            legal_admission = territorial_admission(
                model_document,
                operation="run",
            )
            mapping_nodes = tuple(
                session.scalars(
                    select(ClusterMappingNode)
                    .where(ClusterMappingNode.mapping_id == mapping.id)
                    .order_by(ClusterMappingNode.rank)
                )
            )
            agent_nodes = tuple(
                session.scalars(
                    select(AgentNode).where(
                        AgentNode.node_id.in_(
                            [mapping_node.node_id for mapping_node in mapping_nodes]
                        )
                    )
                )
            )
            agent_capabilities = {
                node.node_id: tuple(node.capabilities or ()) for node in agent_nodes
            }
            receipt_keys = {
                node.node_id: node.observation_receipt_public_key
                for node in agent_nodes
            }
            installed_nodes = {
                (row.node_id, row.rank, row.role)
                for row in session.scalars(
                    select(InstallationNode).where(
                        InstallationNode.installation_id == installation_id,
                        InstallationNode.state == "installed",
                    )
                )
            }
        placements = tuple(
            Placement(item.node_id, item.rank, item.role, item.endpoint_owner)
            for item in mapping_nodes
        )
        capabilities: dict[str, tuple[str, ...]] = {}
        snapshots = {}
        for placement in placements:
            try:
                snapshot = self._inventory.latest(
                    placement.node_id,
                    now=now,
                    maximum_age=self._max_age,
                    _session=_session,
                )
                snapshots[placement.node_id] = snapshot
                capabilities[placement.node_id] = snapshot.capabilities
            except KeyError:
                pass
        topology_reason: AdmissionReason | None = None
        try:
            ordered = validate_topology(revision.document, placements, capabilities)
        except TopologyError as error:
            ordered = tuple(sorted(placements, key=lambda item: item.rank))
            topology_reason = AdmissionReason(error.code, str(error))
        topology = recipe_topology(revision.document)
        topology_roles = topology.get("roles")
        if not isinstance(topology_roles, list):
            raise TypeError("recipe runtime topology is invalid")
        role_by_name = {
            str(role["name"]): role for role in topology_roles if isinstance(role, dict)
        }
        multi_node = len(ordered) > 1
        two_phase_start = multi_node and topology.get("mode") == "distributed"
        endpoint_owner = next(
            (item for item in mapping_nodes if item.endpoint_owner), None
        )
        if endpoint_owner is None:
            raise TypeError("mapping endpoint owner is missing")
        plans: list[RunNodePlan] = []
        fabric_addresses: list[str] = []
        released = tuple(released_run_ids)
        # Both shared ledger projections apply the same owner-scoped visibility
        # predicate; reviewed stops never discount an unrelated owner's claim.
        for placement in ordered:
            blockers = [] if topology_reason is None else [topology_reason]
            warnings: list[AdmissionReason] = []
            if legal_admission.blocker is not None:
                blockers.append(AdmissionReason(*legal_admission.blocker))
            if legal_admission.warning is not None:
                warnings.append(AdmissionReason(*legal_admission.warning))
            snapshot = snapshots.get(placement.node_id)
            if (
                placement.node_id,
                placement.rank,
                placement.role,
            ) not in installed_nodes:
                blockers.append(
                    AdmissionReason(
                        "run.not_installed",
                        "Recipe content is not installed for this mapped rank.",
                    )
                )
            if snapshot is None:
                blockers.append(
                    AdmissionReason(
                        "run.inventory_missing",
                        "No authenticated memory inventory is available.",
                    )
                )
            elif snapshot.stale:
                blockers.append(
                    AdmissionReason(
                        "run.stale_inventory", "GPU node memory inventory is stale."
                    )
                )
            if (
                two_phase_start
                and _DISTRIBUTED_START_CAPABILITY
                not in agent_capabilities.get(placement.node_id, ())
            ):
                blockers.append(
                    AdmissionReason(
                        "run.distributed_start_capability_missing",
                        "Spark agent does not support two-phase distributed start.",
                    )
                )
            if (
                two_phase_start
                and _EXACT_RUN_INSPECTION_CAPABILITY
                not in agent_capabilities.get(placement.node_id, ())
            ):
                blockers.append(
                    AdmissionReason(
                        "run.distributed_observation_capability_missing",
                        "Spark agent does not support exact distributed rank inspection.",
                    )
                )
            if two_phase_start and (
                _SIGNED_RUN_INSPECTION_CAPABILITY
                not in agent_capabilities.get(placement.node_id, ())
                or not isinstance(receipt_keys.get(placement.node_id), str)
            ):
                blockers.append(
                    AdmissionReason(
                        "run.distributed_observation_receipt_capability_missing",
                        "Spark agent does not support signed distributed rank observations.",
                    )
                )
            role = role_by_name.get(placement.role)
            resources = role.get("resources") if isinstance(role, dict) else None
            memory = resources.get("memory") if isinstance(resources, dict) else None
            if not isinstance(memory, dict):
                raise TypeError("topology role memory is invalid")
            memory_need = memory_requirement(
                revision.document,
                memory,
                placement.role,
                model_documents,
                platform_floor_bytes=self._floor,
            )
            required = memory_need.demand.total_bytes
            if required is None:
                raise ValueError(
                    "run memory demand is unavailable for the selected settings"
                )
            memory_floor = memory_need.floor_bytes
            memory_kind = memory_need.kind
            with (
                nullcontext(_session) if _session is not None else self._sessions()
            ) as session:
                reservations = memory_reservations(
                    session,
                    placement.node_id,
                    memory_pool=snapshot.memory_pool if snapshot else None,
                    excluded_run_ids=released,
                    excluded_profile_application_ids=(
                        *excluded_profile_application_ids,
                        *((profile_application_id,) if profile_application_id else ()),
                    ),
                )
                port_demand = run_port_demand(
                    revision.document,
                    node_count=len(ordered),
                    endpoint_owner=placement.endpoint_owner,
                )
                blockers.extend(
                    run_port_blockers(
                        session,
                        placement.node_id,
                        port_demand,
                        excluded_run_ids=released,
                        excluded_profile_application_ids=(
                            *excluded_profile_application_ids,
                            *(
                                (profile_application_id,)
                                if profile_application_id
                                else ()
                            ),
                        ),
                    )
                )
            port = port_demand.plan_port
            rendezvous_port = port_demand.rendezvous_port
            if multi_node and (
                snapshot is None
                or snapshot.fabric_address is None
                or snapshot.fabric_bandwidth_mbps is None
            ):
                blockers.append(
                    AdmissionReason(
                        "run.fabric_address_missing",
                        "Authenticated direct-fabric evidence is unavailable.",
                    )
                )
            if snapshot is not None and snapshot.fabric_address is not None:
                fabric_addresses.append(snapshot.fabric_address)
            capacity = memory_capacity_snapshot(
                placement.node_id,
                memory_need.kind,
                host=(snapshot.host_memory_total_bytes, snapshot.host_memory_free_bytes)
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
                if snapshot is not None and not snapshot.stale
                else "unknown",
                evidence_digest=snapshot.evidence_digest if snapshot else None,
                evidence_observed_at=snapshot.observed_at if snapshot else None,
            )
            reserved = capacity.reserved_bytes or 0
            available = (
                capacity.available_bytes - capacity.occupied_bytes
                if capacity.available_bytes is not None
                and capacity.occupied_bytes is not None
                else None
            )
            memory_fit = plan_capacity(
                {placement.node_id: memory_need.demand},
                [capacity],
                memory_floor_bytes=memory_floor,
            ).nodes[0]
            free_after = memory_fit.selected_free_after_bytes
            for reason in memory_fit.reasons:
                projected = AdmissionReason(
                    "run.insufficient_memory"
                    if reason.code.startswith("resource.insufficient")
                    else reason.code,
                    reason.detail,
                )
                if reason.severity == "blocker":
                    blockers.append(projected)
                else:
                    warnings.append(projected)
            plans.append(
                RunNodePlan(
                    placement.node_id,
                    placement.rank,
                    placement.role,
                    placement.node_id == endpoint_owner.node_id,
                    port,
                    not blockers,
                    snapshot.observed_at if snapshot else None,
                    memory_kind,
                    required,
                    available,
                    reserved,
                    free_after,
                    memory_floor,
                    snapshot.fabric_address if snapshot else None,
                    snapshot.fabric_bandwidth_mbps if snapshot else None,
                    rendezvous_port,
                    tuple(blockers),
                    tuple(warnings),
                    snapshot.memory_pool if snapshot else None,
                )
            )
        if multi_node and len(fabric_addresses) != len(set(fabric_addresses)):
            duplicate = AdmissionReason(
                "run.fabric_address_duplicate",
                "Mapped GPU nodes must have unique direct-fabric addresses.",
            )
            plans = [
                replace(item, allowed=False, blockers=(*item.blockers, duplicate))
                for item in plans
            ]
        identity = {
            "schema_version": 1,
            "installation_id": installation_id,
            "alias": alias,
            "mapping_id": mapping.id,
            "mapping_generation": mapping.generation,
            "recipe_revision_id": revision.id,
            "nodes": [_node_document(item) for item in plans],
        }
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return RunPlan(
            installation_id,
            alias,
            mapping.id,
            mapping.generation,
            revision.id,
            all(item.allowed for item in plans),
            tuple(plans),
            digest,
        )

    def accept_run(self, plan: RunPlan, *, actor: str, now: datetime) -> str:
        with self._sessions.begin() as session:
            return self.accept_run_in_session(session, plan, actor=actor, now=now)

    def accept_run_in_session(
        self,
        session: Session,
        plan: RunPlan,
        *,
        actor: str,
        now: datetime,
        profile_application_id: str | None = None,
        workload_intent_ordinal: int | None = None,
    ) -> str:
        try:
            acquire_admission_keys(
                session,
                tuple(node_admission_key(node.node_id) for node in plan.nodes),
            )
            return self._accept_run_locked_in_session(
                session,
                plan,
                actor=actor,
                now=now,
                profile_application_id=profile_application_id,
                workload_intent_ordinal=workload_intent_ordinal,
            )
        except AdmissionLockBusy as error:
            raise RunAdmissionBusy("run capacity writer is busy") from error
        except OperationalError as error:
            if is_admission_contention(error):
                raise RunAdmissionBusy("run capacity writer is busy") from error
            raise

    def _accept_run_locked_in_session(
        self,
        session: Session,
        plan: RunPlan,
        *,
        actor: str,
        now: datetime,
        profile_application_id: str | None = None,
        workload_intent_ordinal: int | None = None,
    ) -> str:
        node_ids = tuple(node.node_id for node in plan.nodes)
        lock_admission_rows(
            session,
            (
                AdmissionRowLock(
                    "target-agent-nodes",
                    AgentNode,
                    select(AgentNode).where(AgentNode.node_id.in_(node_ids)),
                ),
                AdmissionRowLock(
                    "reviewed-catalog-revision",
                    CatalogDocumentRevision,
                    select(CatalogDocumentRevision).where(
                        CatalogDocumentRevision.id == plan.recipe_revision_id
                    ),
                ),
                AdmissionRowLock(
                    "reviewed-mapping",
                    ClusterMapping,
                    select(ClusterMapping).where(ClusterMapping.id == plan.mapping_id),
                ),
                AdmissionRowLock(
                    "reviewed-installation",
                    RecipeInstallation,
                    select(RecipeInstallation).where(
                        RecipeInstallation.id == plan.installation_id
                    ),
                ),
                AdmissionRowLock(
                    "reviewed-mapping-nodes",
                    ClusterMappingNode,
                    select(ClusterMappingNode).where(
                        ClusterMappingNode.mapping_id == plan.mapping_id
                    ),
                ),
                AdmissionRowLock(
                    "reviewed-installation-nodes",
                    InstallationNode,
                    select(InstallationNode).where(
                        InstallationNode.installation_id == plan.installation_id
                    ),
                ),
                AdmissionRowLock(
                    "node-inventory-snapshots",
                    NodeInventorySnapshot,
                    select(NodeInventorySnapshot).where(
                        NodeInventorySnapshot.node_id.in_(node_ids)
                    ),
                ),
                AdmissionRowLock(
                    "node-resource-reservations",
                    ResourceReservation,
                    select(ResourceReservation).where(
                        ResourceReservation.node_id.in_(node_ids)
                    ),
                ),
            ),
        )
        mapping = session.get(ClusterMapping, plan.mapping_id)
        if (
            mapping is None
            or mapping.state != "ready"
            or mapping.generation != plan.mapping_generation
        ):
            raise RunPlanConflict("mapping generation changed while reserving")
        installation = session.get(RecipeInstallation, plan.installation_id)
        revision = _active_recipe_revision(session, plan.recipe_revision_id)
        mapping_nodes = tuple(
            session.scalars(
                select(ClusterMappingNode)
                .where(ClusterMappingNode.mapping_id == plan.mapping_id)
                .order_by(ClusterMappingNode.rank)
            )
        )
        fresh = self.plan_run(
            plan.installation_id,
            plan.alias,
            now=now,
            _session=session,
            profile_application_id=profile_application_id,
        )
        if (
            not fresh.allowed
            or fresh.plan_digest != plan.plan_digest
            or fresh.mapping_generation != plan.mapping_generation
        ):
            raise RunPlanConflict("run.plan_stale_or_blocked")
        if (
            installation is None
            or installation.mapping_id != plan.mapping_id
            or installation.mapping_generation != plan.mapping_generation
            or revision is None
            or revision.state != "active"
            or tuple(
                (node.node_id, node.rank, node.role, node.endpoint_owner)
                for node in mapping_nodes
            )
            != tuple(
                (node.node_id, node.rank, node.role, node.endpoint_owner)
                for node in plan.nodes
            )
        ):
            raise RunPlanConflict("run.plan_stale")
        try:
            resolve_recipe_entities(session, revision.document)
        except RecipeRuntimeSpecError as error:
            raise RunPlanConflict("run.dependencies_stale") from error
        port_demands = {
            node.node_id: run_port_demand(
                revision.document,
                node_count=len(plan.nodes),
                endpoint_owner=node.endpoint_owner,
            )
            for node in plan.nodes
        }
        inherited_ports = (
            inherited_profile_ports(
                session,
                profile_application_id,
                plan.recipe_revision_id,
                plan.alias,
                {
                    node_id: demand.required_ports
                    for node_id, demand in port_demands.items()
                },
                workload_intent_ordinal=workload_intent_ordinal,
            )
            if profile_application_id is not None
            else {}
        )
        inherited_memory = (
            inherited_profile_memory(
                session,
                profile_application_id,
                plan.recipe_revision_id,
                plan.alias,
                {
                    node.node_id: (
                        node.memory_kind,
                        node.required_memory_bytes,
                        node.memory_pool,
                    )
                    for node in plan.nodes
                },
                workload_intent_ordinal=workload_intent_ordinal,
            )
            if profile_application_id is not None
            else {}
        )
        logical_job = next(iter(port_demands.values())).logical_job
        # Exact signed observation is the sole current run contract for every
        # topology.  Singleton runs retain a durable generation while their
        # rendezvous fields remain explicitly nullable.
        observation_schema_version = 2
        try:
            persisted_plan = run_plan_document(
                {
                    "schema_version": 1,
                    "observation_schema_version": observation_schema_version,
                    "run_generation": 1,
                    "installation_id": plan.installation_id,
                    "alias": plan.alias,
                    "mapping_id": plan.mapping_id,
                    "mapping_generation": plan.mapping_generation,
                    "recipe_revision_id": plan.recipe_revision_id,
                    "plan_digest": plan.plan_digest,
                    "nodes": [_node_document(item) for item in plan.nodes],
                    "execution_mode": "one-shot-jobs" if logical_job else None,
                }
            )
        except RecipeExecutionContractError as error:
            raise RunPlanConflict("run.plan_invalid") from error
        run = RecipeRun(
            installation_id=plan.installation_id,
            mapping_id=plan.mapping_id,
            mapping_generation=plan.mapping_generation,
            run_generation=1,
            alias=plan.alias,
            plan_digest=plan.plan_digest,
            plan=persisted_plan,
            state="planned",
            route_state="withdrawn",
            actor=actor,
            created_at=now,
            updated_at=now,
        )
        session.add(run)
        session.flush()
        for node in plan.nodes:
            session.add(
                RunNode(
                    run_id=run.id,
                    node_id=node.node_id,
                    rank=node.rank,
                    role=node.role,
                    state="planned",
                    port=node.port,
                    reserved_memory_bytes=node.required_memory_bytes,
                    updated_at=now,
                )
            )
            memory_kind = memory_reservation_kind(node.memory_kind)
            inherited = inherited_memory.get(node.node_id)
            if inherited is not None:
                inherited.owner_kind = "run"
                inherited.owner_id = run.id
                inherited.resource_key = plan.plan_digest
                inherited.plan_digest = plan.plan_digest
                inherited.state = "active"
            else:
                session.add(
                    ResourceReservation(
                        node_id=node.node_id,
                        kind=memory_kind,
                        resource_key=plan.plan_digest,
                        amount_bytes=node.required_memory_bytes,
                        owner_kind="run",
                        owner_id=run.id,
                        state="active",
                        plan_digest=plan.plan_digest,
                        created_at=now,
                    )
                )
            for reserved_port in port_demands[node.node_id].required_ports:
                inherited = inherited_ports.get((node.node_id, reserved_port))
                if inherited is not None:
                    inherited.owner_kind = "run"
                    inherited.owner_id = run.id
                    inherited.plan_digest = plan.plan_digest
                    inherited.state = "active"
                    continue
                session.add(
                    ResourceReservation(
                        node_id=node.node_id,
                        kind="port",
                        resource_key=str(reserved_port),
                        amount_bytes=0,
                        owner_kind="run",
                        owner_id=run.id,
                        state="active",
                        plan_digest=plan.plan_digest,
                        created_at=now,
                    )
                )
        return run.id


def _node_document(node: RunNodePlan) -> dict[str, object]:
    return {
        **asdict(node),
        "inventory_observed_at": (
            node.inventory_observed_at.isoformat()
            if node.inventory_observed_at
            else None
        ),
    }
