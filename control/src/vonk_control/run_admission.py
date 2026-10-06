"""Mapping-fenced memory, port, capability, and fabric admission."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Iterable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import asdict, dataclass, replace
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    InstallationNodeState,
    InstallationState,
    InvalidRequestError,
    InvalidRequestReason,
    ReservationState,
    ResourceBlockerCode,
    RouteState,
    RunAdmissionCode,
    RunState,
    UnknownOutcomeError,
)
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
from .categorized_errors import InvalidType, InvalidValue, MissingRecord
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
from .platform_ports import RENDEZVOUS_PORT, service_host_port_candidates
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

_PORT_CONFLICTS = {
    "service": (
        RunAdmissionCode.PORT_OCCUPIED,
        "Port {port} is already reserved on this GPU node.",
    ),
    "rendezvous": (
        RunAdmissionCode.RENDEZVOUS_PORT_OCCUPIED,
        "Multi-node rendezvous port {port} is already reserved.",
    ),
}
PORT_ADMISSION_CODES = frozenset(code for code, _ in _PORT_CONFLICTS.values())


@dataclass(frozen=True, slots=True)
class RunPortDemand:
    """The ports one mapped rank requires, including before installation.

    ``service_candidates`` are the host ports the endpoint may take, in
    allocation order; ``service_port`` is the one chosen for this rank (the
    first candidate until ``allocated`` picks a free one). A logical job has
    neither.
    """

    service_candidates: tuple[int, ...]
    rendezvous_port: int | None
    service_port: int | None = None

    def __post_init__(self) -> None:
        if self.service_port is None and self.service_candidates:
            object.__setattr__(self, "service_port", self.service_candidates[0])

    @property
    def logical_job(self) -> bool:
        return not self.service_candidates

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

    def with_service_port(self, port: int) -> RunPortDemand:
        return replace(self, service_port=port)


def run_port_demand(
    document: Mapping[str, object], *, node_count: int, endpoint_owner: bool
) -> RunPortDemand:
    interfaces = document.get("interfaces")
    if not isinstance(interfaces, list):
        raise InvalidType("recipe interfaces are invalid")
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
            raise InvalidType("artifact job recipes currently require one node")
        return RunPortDemand((), None)
    port = interface.get("port") if interface is not None else None
    if type(port) is not int or not 1 <= port <= 65535:
        raise InvalidType("recipe interface port is invalid")
    return RunPortDemand(
        service_host_port_candidates(port, node_count=node_count),
        RENDEZVOUS_PORT if node_count > 1 and endpoint_owner else None,
    )


def allocate_service_port(
    session: Session,
    node_id: str,
    demand: RunPortDemand,
    *,
    excluded_run_ids: Sequence[str] = (),
    excluded_profile_application_ids: Sequence[str] = (),
) -> RunPortDemand:
    """Choose the first authorised endpoint host port not reserved on the node.

    The platform owns host ports: a recipe's declared port is only the
    container port. Every caller that admits or plans a run resolves its demand
    here so that review, admission and the reservation agree. When every
    candidate is reserved the first stays chosen and ``run_port_blockers``
    reports it.
    """

    if len(demand.service_candidates) < 2:
        return demand
    keys = tuple(str(port) for port in demand.service_candidates)
    occupied = set(
        session.scalars(
            select(ResourceReservation.resource_key).where(
                ResourceReservation.node_id == node_id,
                ResourceReservation.kind == "port",
                ResourceReservation.state.in_(
                    (ReservationState.ACTIVE, ReservationState.PROMISED)
                ),
                reservation_visible(
                    excluded_profile_application_ids,
                    excluded_run_ids=excluded_run_ids,
                ),
                ResourceReservation.resource_key.in_(keys),
            )
        )
    )
    # A reviewed profile promised a specific port to this load. The promise is
    # what the run must consume, so it is chosen first even when an earlier
    # candidate has since been released.
    promised = (
        set(
            session.scalars(
                select(ResourceReservation.resource_key).where(
                    ResourceReservation.node_id == node_id,
                    ResourceReservation.kind == "port",
                    ResourceReservation.state == ReservationState.PROMISED,
                    ResourceReservation.owner_kind == "fleet-profile",
                    ResourceReservation.owner_id.in_(
                        tuple(excluded_profile_application_ids)
                    ),
                    ResourceReservation.resource_key.in_(keys),
                )
            )
        )
        if excluded_profile_application_ids
        else set()
    )
    ordered = sorted(
        demand.service_candidates, key=lambda port: (str(port) not in promised,)
    )
    free = next((port for port in ordered if str(port) not in occupied), None)
    return demand if free is None else demand.with_service_port(free)


def run_port_blockers(
    session: Session,
    node_id: str,
    demand: RunPortDemand,
    *,
    excluded_run_ids: Sequence[str] = (),
    excluded_profile_application_ids: Sequence[str] = (),
) -> tuple[AdmissionReason, ...]:
    wanted = {*demand.service_candidates, *demand.required_ports}
    reservations = session.scalars(
        select(ResourceReservation).where(
            ResourceReservation.node_id == node_id,
            ResourceReservation.kind == "port",
            ResourceReservation.state.in_(
                (ReservationState.ACTIVE, ReservationState.PROMISED)
            ),
            reservation_visible(
                excluded_profile_application_ids, excluded_run_ids=excluded_run_ids
            ),
            ResourceReservation.resource_key.in_(tuple(str(port) for port in wanted)),
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
        elif (
            kind == "service"
            and len(demand.service_candidates) > 1
            and all(str(item) in occupied for item in demand.service_candidates)
        ):
            taken = " and ".join(str(item) for item in demand.service_candidates)
            blockers.append(
                AdmissionReason(
                    code,
                    f"Every endpoint port ({taken}) is already reserved on this GPU node.",
                )
            )
        elif (kind == "rendezvous" or len(demand.service_candidates) <= 1) and str(
            port
        ) in occupied:
            blockers.append(AdmissionReason(code, detail.format(port=port)))
    return tuple(blockers)


class RunPlanConflict(RuntimeError):
    code = RunAdmissionCode.PLAN_INVALID


class RunPlanInvalid(InvalidRequestError, RunPlanConflict):
    """The run plan is stale, invalid or blocked by admission evidence: the
    request is rejected and asked again against the current plan."""

    def __init__(
        self,
        *args: object,
        reason: InvalidRequestReason | None = InvalidRequestReason.CONFLICT,
    ) -> None:
        super().__init__(*args, reason=reason)


class RunAdmissionBusy(UnknownOutcomeError, RunPlanConflict):
    """A competing capacity writer or retryable plan blocker holds this admission.

    The class code ``run.capacity_busy`` names lock contention only.  Any other
    reason the same admission must be retried carries its own typed code (and
    the blocker list it was derived from), so the waiting operation can show the
    real cause instead of a generic busy writer.
    """

    code = RunAdmissionCode.CAPACITY_BUSY

    def __init__(
        self,
        message: str = "",
        *,
        code: str | None = None,
        blockers: Sequence[AdmissionReason] = (),
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.blockers = tuple(blockers)

    @property
    def detail(self) -> str | None:
        """The underlying cause for a wait; ``None`` for plain lock contention."""

        if self.blockers:
            return _blocker_text(self.blockers)
        return str(self) if self.code != RunAdmissionBusy.code else None


def _blocker_text(blockers: Iterable[AdmissionReason]) -> str:
    return "; ".join(f"{item.code}: {item.detail}" for item in blockers)


#: Typed non-contention waits a run admission can name besides plan blockers.
RUN_ADMISSION_WAIT_CODES = (
    RunAdmissionCode.TARGET_MEMBERSHIP_CHANGED,
    RunAdmissionCode.MAPPING_NOT_READY,
)

RETRYABLE_PLAN_BLOCKERS = frozenset(
    {
        RunAdmissionCode.INVENTORY_MISSING,
        RunAdmissionCode.STALE_INVENTORY,
        RunAdmissionCode.INSUFFICIENT_MEMORY,
        RunAdmissionCode.PORT_OCCUPIED,
        RunAdmissionCode.RENDEZVOUS_PORT_OCCUPIED,
        ResourceBlockerCode.INSUFFICIENT,
    }
)


def require_admissible(plan: RunPlan) -> None:
    if plan.allowed:
        return
    blockers = tuple(reason for item in plan.nodes for reason in item.blockers)
    codes = {reason.code for reason in blockers}
    if codes and codes <= RETRYABLE_PLAN_BLOCKERS:
        # Keep the first blocker's own typed code and every detail: this is a
        # wait on named evidence (memory, port, inventory), not a busy writer.
        first = min(blockers, key=lambda reason: reason.code)
        raise RunAdmissionBusy(
            f"run is waiting for current inventory or capacity: {_blocker_text(blockers)}",
            code=first.code,
            blockers=blockers,
        )
    raise RunPlanInvalid(
        "run.plan_invalid: run plan is blocked by current admission evidence"
        + (f" ({_blocker_text(blockers)})" if blockers else "")
    )


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
                raise MissingRecord(installation_id)
            if installation.state != InstallationState.INSTALLED:
                raise InvalidValue(
                    "recipe installation is not complete",
                    reason=InvalidRequestReason.NOT_READY,
                )
            mapping = session.get(ClusterMapping, installation.mapping_id)
            if (
                mapping is None
                or mapping.state != "ready"
                or mapping.generation != installation.mapping_generation
            ):
                raise InvalidValue(
                    "cluster mapping generation changed after installation",
                    reason=InvalidRequestReason.CONFLICT,
                )
            revision = _active_recipe_revision(session, installation.recipe_revision_id)
            if revision is None or revision.state != "active":
                raise InvalidValue(
                    "recipe revision is unavailable",
                    reason=InvalidRequestReason.NOT_FOUND,
                )
            try:
                resolved_entities = resolve_recipe_entities(session, revision.document)
            except RecipeRuntimeSpecError as error:
                raise InvalidValue(
                    "exact recipe dependencies are unavailable",
                    reason=InvalidRequestReason.NOT_FOUND,
                ) from error
            resolved_models = resolved_entities.model_revisions
            model_documents = {
                (item.publisher, item.slug, item.content_digest): item.document
                for item in resolved_models
            }
            model_version = resolved_models[0] if resolved_models else None
            model_document = model_version.document if model_version else None
            if not isinstance(model_document, Mapping):
                raise InvalidValue(
                    "exact model license authority is unavailable",
                    reason=InvalidRequestReason.NOT_FOUND,
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
            unreconciled_lost_ranks: dict[str, list[tuple[str, str]]] = {}
            for node_id, run_id, run_alias in session.execute(
                select(RunNode.node_id, RecipeRun.id, RecipeRun.alias)
                .join(RecipeRun, RecipeRun.id == RunNode.run_id)
                .where(
                    RunNode.node_id.in_(
                        [mapping_node.node_id for mapping_node in mapping_nodes]
                    ),
                    RunNode.state != RunState.STOPPED,
                    RecipeRun.state == RunState.LOST,
                    # A reviewed plan that stops the lost run reconciles it.
                    RecipeRun.id.not_in(tuple(released_run_ids)),
                )
                .order_by(RunNode.node_id, RecipeRun.id)
            ):
                unreconciled_lost_ranks.setdefault(node_id, []).append(
                    (run_id, run_alias)
                )
            installed_nodes = {
                (row.node_id, row.rank, row.role)
                for row in session.scalars(
                    select(InstallationNode).where(
                        InstallationNode.installation_id == installation_id,
                        InstallationNode.state == InstallationNodeState.INSTALLED,
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
        role_by_name = {role.name: role for role in topology.roles}
        multi_node = len(ordered) > 1
        endpoint_owner = next(
            (item for item in mapping_nodes if item.endpoint_owner), None
        )
        if endpoint_owner is None:
            raise InvalidType(
                "mapping endpoint owner is missing",
                reason=InvalidRequestReason.NOT_FOUND,
            )
        plans: list[RunNodePlan] = []
        fabric_addresses: list[str] = []
        released = tuple(released_run_ids)
        # Both shared ledger projections apply the same owner-scoped visibility
        # predicate; reviewed stops never discount an unrelated owner's claim.
        for placement in ordered:
            blockers = [] if topology_reason is None else [topology_reason]
            warnings: list[AdmissionReason] = []
            for run_id, run_alias in unreconciled_lost_ranks.get(placement.node_id, ()):
                blockers.append(
                    AdmissionReason(
                        RunAdmissionCode.UNRECONCILED_LOST_RANK,
                        f"Spark {placement.node_id} still has rank state for lost "
                        f"model {run_alias} ({run_id}); reconcile it before placing work.",
                    )
                )
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
                        RunAdmissionCode.NOT_INSTALLED,
                        "Recipe content is not installed for this mapped rank.",
                    )
                )
            if snapshot is None:
                blockers.append(
                    AdmissionReason(
                        RunAdmissionCode.INVENTORY_MISSING,
                        "No authenticated memory inventory is available.",
                    )
                )
            elif snapshot.stale:
                blockers.append(
                    AdmissionReason(
                        RunAdmissionCode.STALE_INVENTORY,
                        "GPU node memory inventory is stale.",
                    )
                )
            role = role_by_name.get(placement.role)
            if role is None:
                raise InvalidType("topology role memory is invalid")
            memory_need = memory_requirement(
                revision.document,
                role.resources.memory,
                placement.role,
                model_documents,
                platform_floor_bytes=self._floor,
            )
            required = memory_need.demand.total_bytes
            if required is None:
                raise InvalidValue(
                    "run memory demand is unavailable for the selected settings",
                    reason=InvalidRequestReason.NOT_FOUND,
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
                    observed_at=snapshot.observed_at if snapshot else None,
                )
                port_exclusions = {
                    "excluded_run_ids": released,
                    "excluded_profile_application_ids": (
                        *excluded_profile_application_ids,
                        *((profile_application_id,) if profile_application_id else ()),
                    ),
                }
                port_demand = allocate_service_port(
                    session,
                    placement.node_id,
                    run_port_demand(
                        revision.document,
                        node_count=len(ordered),
                        endpoint_owner=placement.endpoint_owner,
                    ),
                    **port_exclusions,
                )
                blockers.extend(
                    run_port_blockers(
                        session, placement.node_id, port_demand, **port_exclusions
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
                        RunAdmissionCode.FABRIC_ADDRESS_MISSING,
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
                    RunAdmissionCode.INSUFFICIENT_MEMORY
                    if reason.code.startswith(ResourceBlockerCode.INSUFFICIENT)
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
                RunAdmissionCode.FABRIC_ADDRESS_DUPLICATE,
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
            plan = self.plan_run(
                plan.installation_id,
                plan.alias,
                now=now,
                _session=session,
                profile_application_id=profile_application_id,
            )
            require_admissible(plan)
            acquire_admission_keys(
                session,
                tuple(node_admission_key(node.node_id) for node in plan.nodes),
                holder="run-admission",
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
        require_admissible(fresh)
        if {node.node_id for node in fresh.nodes} != set(node_ids):
            raise RunAdmissionBusy(
                "run target membership changed during admission",
                code=RunAdmissionCode.TARGET_MEMBERSHIP_CHANGED,
            )
        plan = fresh
        if mapping is None or mapping.state != "ready":
            raise RunAdmissionBusy(
                "run mapping is waiting to become ready",
                code=RunAdmissionCode.MAPPING_NOT_READY,
            )
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
            raise RunPlanInvalid(
                RunAdmissionCode.PLAN_STALE, reason=InvalidRequestReason.SUPERSEDED
            )
        try:
            resolve_recipe_entities(session, revision.document)
        except RecipeRuntimeSpecError as error:
            raise RunPlanInvalid(
                RunAdmissionCode.DEPENDENCIES_STALE,
                reason=InvalidRequestReason.SUPERSEDED,
            ) from error
        # The fresh plan above chose each node's endpoint host port under the
        # node locks this admission holds; reserve exactly that port.
        port_demands = {
            node.node_id: _planned_port_demand(
                run_port_demand(
                    revision.document,
                    node_count=len(plan.nodes),
                    endpoint_owner=node.endpoint_owner,
                ),
                node,
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
        # Exact observation is the sole current run contract for every
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
            raise RunPlanInvalid(RunAdmissionCode.PLAN_INVALID) from error
        run = RecipeRun(
            installation_id=plan.installation_id,
            mapping_id=plan.mapping_id,
            mapping_generation=plan.mapping_generation,
            run_generation=1,
            alias=plan.alias,
            plan_digest=plan.plan_digest,
            plan=persisted_plan,
            state=RunState.PLANNED,
            route_state=RouteState.WITHDRAWN,
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
                    state=RunState.PLANNED,
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
                        state=ReservationState.ACTIVE,
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
                        state=ReservationState.ACTIVE,
                        plan_digest=plan.plan_digest,
                        created_at=now,
                    )
                )
        return run.id


def _planned_port_demand(demand: RunPortDemand, node: RunNodePlan) -> RunPortDemand:
    return demand if demand.logical_job else demand.with_service_port(node.port)


def _node_document(node: RunNodePlan) -> dict[str, object]:
    return {
        **asdict(node),
        "inventory_observed_at": (
            node.inventory_observed_at.isoformat()
            if node.inventory_observed_at
            else None
        ),
    }
