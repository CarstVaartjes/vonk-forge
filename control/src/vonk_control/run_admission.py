"""Mapping-fenced memory, port, capability, and fabric admission."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import asdict, dataclass, replace
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .install_admission import AdmissionReason
from .inventory_repository import InventoryRepository
from .legal_admission import territorial_admission
from .models import (
    AgentNode,
    ClusterMapping,
    ClusterMappingNode,
    InstallationNode,
    LocalRecipeRevision,
    NodeInventorySnapshot,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from .recipe_contract import recipe_topology
from .recipe_runtime_specs import RecipeRuntimeSpecError, resolve_recipe_entities
from .topology import Placement, TopologyError, validate_topology

_DISTRIBUTED_START_CAPABILITY = "recipe.start.two-phase.v1"
_EXACT_RUN_INSPECTION_CAPABILITY = "recipe.run.inspect.exact.v1"
_SIGNED_RUN_INSPECTION_CAPABILITY = "recipe.run.inspect.receipt.v1"


class RunPlanConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RunNodePlan:
    node_id: str
    rank: int
    role: str
    endpoint_owner: bool
    port: int
    allowed: bool
    inventory_observed_at: datetime | None
    memory_kind: str
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
        memory_floor_bytes: int = 4_000_000_000,
        operator_jurisdiction: str | None = None,
    ) -> None:
        self._sessions = sessions
        self._inventory = InventoryRepository(sessions)
        self._max_age = inventory_max_age
        self._floor = memory_floor_bytes
        # Territorial declarations remain informational model metadata.  The
        # run plan deliberately does not derive an operator location or enforce
        # a territory denial.  Keep accepting the constructor keyword while
        # callers converge on the geography-free admission contract.
        del operator_jurisdiction

    def plan_run(
        self,
        installation_id: str,
        alias: str,
        *,
        now: datetime,
        _session: Session | None = None,
    ) -> RunPlan:
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
            revision = session.get(LocalRecipeRevision, installation.recipe_revision_id)
            if revision is None or revision.lifecycle != "resolved":
                raise ValueError("recipe revision is unavailable")
            try:
                resolved_entities = resolve_recipe_entities(session, revision.document)
            except RecipeRuntimeSpecError as error:
                raise ValueError("exact recipe dependencies are unavailable") from error
            model_version = resolved_entities.get("model_version")
            model_document = getattr(model_version, "document", None)
            if not isinstance(model_document, Mapping):
                raise ValueError(  # noqa: TRY004
                    "exact model license authority is unavailable"
                )
            legal_admission = territorial_admission(
                model_document,
                None,
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
        interfaces = revision.document.get("interfaces")
        if not isinstance(topology_roles, list) or not isinstance(interfaces, list):
            raise TypeError("recipe runtime topology is invalid")
        role_by_name = {
            str(role["name"]): role for role in topology_roles if isinstance(role, dict)
        }
        interface = next(
            (
                value
                for value in interfaces
                if isinstance(value, dict) and value.get("adapter") == "openai"
            ),
            None,
        )
        artifact_interfaces = [
            value
            for value in interfaces
            if isinstance(value, dict)
            and value.get("adapter")
            in {"audio-job", "video-job", "image-job", "mesh-job", "artifact-job"}
        ]
        logical_job = interface is None and len(artifact_interfaces) == 1
        if logical_job:
            if len(ordered) != 1:
                raise TypeError("artifact job recipes currently require one node")
            port = 1024
        elif not isinstance(interface, dict) or not isinstance(
            interface.get("port"), int
        ):
            raise TypeError("recipe interface is invalid")
        else:
            port = int(interface["port"])
        multi_node = len(ordered) > 1
        two_phase_start = multi_node and topology.get("mode") == "distributed"
        endpoint_owner = next(
            (item for item in mapping_nodes if item.endpoint_owner), None
        )
        if endpoint_owner is None:
            raise TypeError("mapping endpoint owner is missing")
        plans: list[RunNodePlan] = []
        fabric_addresses: list[str] = []
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
            required = max(
                int(memory["startup_peak_bytes"]),
                int(memory["steady_state_bytes"]) + int(memory["runtime_growth_bytes"]),
            )
            memory_floor = max(
                self._floor,
                int(memory["system_reserve_bytes"]),
            )
            memory_kind = str(memory["kind"])
            reservation_kind = {
                "unified": "unified-memory",
                "host": "host-memory",
                "accelerator": "gpu-memory",
            }[memory_kind]
            with (
                nullcontext(_session) if _session is not None else self._sessions()
            ) as session:
                reserved = int(
                    session.scalar(
                        select(
                            func.coalesce(func.sum(ResourceReservation.amount_bytes), 0)
                        ).where(
                            ResourceReservation.node_id == placement.node_id,
                            ResourceReservation.kind == reservation_kind,
                            ResourceReservation.state == "active",
                        )
                    )
                    or 0
                )
                occupied = (
                    None
                    if logical_job
                    else session.scalar(
                        select(ResourceReservation.id).where(
                            ResourceReservation.node_id == placement.node_id,
                            ResourceReservation.kind == "port",
                            ResourceReservation.resource_key == str(port),
                            ResourceReservation.state == "active",
                        )
                    )
                )
                rendezvous_occupied = (
                    session.scalar(
                        select(ResourceReservation.id).where(
                            ResourceReservation.node_id == placement.node_id,
                            ResourceReservation.kind == "port",
                            ResourceReservation.resource_key == "29500",
                            ResourceReservation.state == "active",
                        )
                    )
                    if multi_node and placement.node_id == endpoint_owner.node_id
                    else None
                )
            if occupied is not None:
                blockers.append(
                    AdmissionReason(
                        "run.port_occupied",
                        f"Port {port} is already reserved on this GPU node.",
                    )
                )
            rendezvous_port = (
                29500
                if multi_node and placement.node_id == endpoint_owner.node_id
                else None
            )
            if rendezvous_port == port or rendezvous_occupied is not None:
                blockers.append(
                    AdmissionReason(
                        "run.rendezvous_port_occupied",
                        "Multi-node rendezvous port 29500 is already reserved.",
                    )
                )
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
            available = (
                None
                if snapshot is None
                else min(
                    snapshot.host_memory_free_bytes,
                    snapshot.gpu_memory_free_bytes,
                )
                if memory_kind == "unified"
                else snapshot.host_memory_free_bytes
                if memory_kind == "host"
                else snapshot.gpu_memory_free_bytes
            )
            free_after = None if available is None else available - reserved - required
            if free_after is not None and free_after < memory_floor:
                blockers.append(
                    AdmissionReason(
                        "run.insufficient_memory",
                        f"Run would leave {free_after} bytes, below the {memory_floor}-byte memory floor.",
                    )
                )
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
    ) -> str:
        mapping = session.get(ClusterMapping, plan.mapping_id, with_for_update=True)
        if (
            mapping is None
            or mapping.state != "ready"
            or mapping.generation != plan.mapping_generation
        ):
            raise RunPlanConflict("mapping generation changed while reserving")
        installation = session.get(
            RecipeInstallation, plan.installation_id, with_for_update=True
        )
        revision = session.get(
            LocalRecipeRevision, plan.recipe_revision_id, with_for_update=True
        )
        mapping_nodes = tuple(
            session.scalars(
                select(ClusterMappingNode)
                .where(ClusterMappingNode.mapping_id == plan.mapping_id)
                .order_by(ClusterMappingNode.rank)
                .with_for_update()
            )
        )
        node_ids = tuple(node.node_id for node in mapping_nodes)
        session.scalars(
            select(AgentNode).where(AgentNode.node_id.in_(node_ids)).with_for_update()
        ).all()
        session.scalars(
            select(InstallationNode)
            .where(InstallationNode.installation_id == plan.installation_id)
            .with_for_update()
        ).all()
        session.scalars(
            select(ResourceReservation)
            .where(ResourceReservation.node_id.in_(node_ids))
            .with_for_update()
        ).all()
        session.scalars(
            select(NodeInventorySnapshot)
            .where(NodeInventorySnapshot.node_id.in_(node_ids))
            .with_for_update()
        ).all()
        fresh = self.plan_run(
            plan.installation_id, plan.alias, now=now, _session=session
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
            or revision.lifecycle != "resolved"
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
        interfaces = revision.document.get("interfaces")
        logical_job = (
            isinstance(interfaces, list)
            and len(interfaces) == 1
            and isinstance(interfaces[0], Mapping)
            and interfaces[0].get("adapter")
            in {"audio-job", "video-job", "image-job", "mesh-job", "artifact-job"}
        )
        topology = recipe_topology(revision.document)
        observation_schema_version = (
            2 if len(plan.nodes) > 1 and topology.get("mode") == "distributed" else 1
        )
        run = RecipeRun(
            installation_id=plan.installation_id,
            mapping_id=plan.mapping_id,
            mapping_generation=plan.mapping_generation,
            run_generation=1,
            alias=plan.alias,
            plan_digest=plan.plan_digest,
            plan={
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
            },
            state="planned",
            route_state="withdrawn",
            actor=actor,
            created_at=now,
            updated_at=now,
        )
        ordered = tuple(sorted(plan.nodes, key=lambda node: node.node_id))
        for node in ordered:
            if (
                session.scalar(
                    select(AgentNode)
                    .where(AgentNode.node_id == node.node_id)
                    .with_for_update()
                )
                is None
            ):
                raise RunPlanConflict("run node disappeared")
            snapshot = session.scalar(
                select(NodeInventorySnapshot)
                .where(NodeInventorySnapshot.node_id == node.node_id)
                .order_by(NodeInventorySnapshot.observed_at.desc())
                .limit(1)
                .with_for_update()
            )
            if snapshot is None:
                raise RunPlanConflict("run inventory disappeared")
            reservation_kind = {
                "unified": "unified-memory",
                "host": "host-memory",
                "accelerator": "gpu-memory",
            }[node.memory_kind]
            reserved = int(
                session.scalar(
                    select(
                        func.coalesce(func.sum(ResourceReservation.amount_bytes), 0)
                    ).where(
                        ResourceReservation.node_id == node.node_id,
                        ResourceReservation.kind == reservation_kind,
                        ResourceReservation.state == "active",
                    )
                )
                or 0
            )
            if (
                node.available_memory_bytes is None
                or node.available_memory_bytes - reserved - node.required_memory_bytes
                < self._floor
            ):
                raise RunPlanConflict("memory capacity changed while reserving")
            ports = (
                ()
                if logical_job
                else (
                    (node.port,)
                    if node.rendezvous_port is None
                    else (node.port, node.rendezvous_port)
                )
            )
            for reserved_port in ports:
                if (
                    session.scalar(
                        select(ResourceReservation.id).where(
                            ResourceReservation.node_id == node.node_id,
                            ResourceReservation.kind == "port",
                            ResourceReservation.resource_key == str(reserved_port),
                            ResourceReservation.state == "active",
                        )
                    )
                    is not None
                ):
                    raise RunPlanConflict("run port changed while reserving")
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
            memory_kind = {
                "unified": "unified-memory",
                "host": "host-memory",
                "accelerator": "gpu-memory",
            }[node.memory_kind]
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
            ports = (
                ()
                if logical_job
                else (
                    (node.port,)
                    if node.rendezvous_port is None
                    else (node.port, node.rendezvous_port)
                )
            )
            for reserved_port in ports:
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
