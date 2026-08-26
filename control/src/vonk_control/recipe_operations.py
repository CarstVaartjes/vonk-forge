"""Digest-bound orchestration for local recipe installation and execution."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import canonical_message

from .cluster_mappings import ClusterMappingPlan, ClusterMappingService
from .distributed_lifecycle import DistributedLifecycleError
from .distributed_recovery import enforce_recovery_deadline, recovery_start_plan
from .install_admission import InstallAdmissionService, InstallPlan
from .models import (
    AgentOperation,
    AgentPresence,
    InstallationNode,
    Job,
    LocalRecipeRevision,
    NodeArtifact,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from .recipe_action_plans import (
    StopNodeImpact,
    StopPlan,
    UninstallActiveRun,
    UninstallNodeImpact,
    UninstallPlan,
    stop_plan,
    uninstall_plan,
)
from .recipe_builds import RecipeBuildPlan, RecipeBuildService
from .recipe_contract import recipe_topology
from .recipe_routes import RecipeRouteService, route_publication_transaction
from .run_admission import RunAdmissionService, RunPlan
from .source_policy import SourcePolicyReport


class AgentJobQueue(Protocol):
    def enqueue_in_session(
        self,
        session: Session,
        parent_job_id: str,
        node_id: str,
        operation: str,
        authority_revision: str,
        payload: Mapping[str, object],
        *,
        operation_id: str,
    ) -> AgentOperation: ...

    def notify_available(self) -> None: ...


class RecipeOperationConflict(RuntimeError):
    """A lifecycle request is stale, conflicting, or unsafe to execute."""


@dataclass(frozen=True, slots=True)
class RecipeOperationView:
    id: str
    kind: str
    owner_id: str
    state: str
    plan_digest: str
    nodes: tuple[str, ...]
    result: dict[str, object] | None


@dataclass(frozen=True, slots=True)
class ImageDistributionPreview:
    recipe_build_id: str
    mapping_id: str
    mapping_generation: int
    image_digest: str
    node_ids: tuple[str, ...]
    plan_digest: str


@dataclass(frozen=True, slots=True)
class RecipeRunRankStatus:
    node_id: str
    rank: int
    role: str
    state: str
    observed_at: datetime
    age_seconds: float
    fresh: bool


@dataclass(frozen=True, slots=True)
class RecipeRunStatus:
    id: str
    alias: str
    state: str
    route_state: str
    healthy: bool
    ranks: tuple[RecipeRunRankStatus, ...]


@dataclass(frozen=True, slots=True)
class RecipeRunObservation:
    run_id: str
    ready: bool

_TERMINAL_JOB_STATES = frozenset({"succeeded", "failed", "expired", "cancelled"})
_RETRYABLE_IMAGE_DISTRIBUTION_STATES = frozenset({"failed", "waiting-for-operator"})
_RETRYABLE_IMAGE_OPERATION_STATES = _TERMINAL_JOB_STATES | frozenset(
    {"waiting-for-operator"}
)
_MEMORY_RESERVATION_KINDS = frozenset({"unified-memory", "host-memory", "gpu-memory"})
_MAX_ACTION_NODES = 1024
_MAX_ACTIVE_RUNS = 128


def _cancel_reason(value: object) -> str:
    reason = " ".join(str(value).split())
    if not reason:
        raise RecipeOperationConflict("cancellation reason is required")
    return reason[:512]


class RecipeOperationService:
    """Turn accepted admission plans into one fenced, gang-aware operation group."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        install_admission: InstallAdmissionService,
        run_admission: RunAdmissionService,
        agent_jobs: AgentJobQueue,
        clock: Callable[[], datetime],
        route_withdrawer: Callable[[str], None] | None = None,
        route_publications: RecipeRouteService | None = None,
        builds: RecipeBuildService | None = None,
        mappings: ClusterMappingService | None = None,
        run_health_maximum_age_seconds: int = 300,
    ) -> None:
        if not 1 <= run_health_maximum_age_seconds <= 300:
            raise ValueError("recipe run health age is invalid")
        self._sessions = sessions
        self._install_admission = install_admission
        self._run_admission = run_admission
        self._agent_jobs = agent_jobs
        self._clock = clock
        self._route_withdrawer = route_withdrawer or (lambda _run_id: None)
        self._route_publications = route_publications
        self._builds = builds
        self._mappings = mappings
        self._run_health_maximum_age = timedelta(seconds=run_health_maximum_age_seconds)

    def preview_mapping(
        self,
        recipe_revision_id: str,
        node_ids: tuple[str, ...],
        *,
        parameters: Mapping[str, object],
        actor: str,
    ) -> ClusterMappingPlan:
        if self._mappings is None:
            raise RecipeOperationConflict("cluster mapping service is unavailable")
        return self._mappings.preview(recipe_revision_id, node_ids, parameters, actor)

    def create_mapping(self, plan: ClusterMappingPlan, *, actor: str) -> str:
        if self._mappings is None:
            raise RecipeOperationConflict("cluster mapping service is unavailable")
        try:
            return self._mappings.materialize(plan, actor=actor, now=self._clock())
        except (RuntimeError, ValueError) as error:
            raise RecipeOperationConflict(str(error)) from error

    def preview_build(
        self, recipe_revision_id: str, builder_node_id: str
    ) -> RecipeBuildPlan:
        if self._builds is None:
            raise RecipeOperationConflict("recipe build service is unavailable")
        return self._builds.plan(recipe_revision_id, builder_node_id, now=self._clock())

    def check_build_source(self, recipe_revision_id: str) -> SourcePolicyReport:
        if self._builds is None:
            raise RecipeOperationConflict("recipe build service is unavailable")
        return self._builds.check_source(recipe_revision_id)

    def build(
        self,
        plan: RecipeBuildPlan,
        *,
        build_input_sha256: str,
        actor: str,
        request_id: str,
    ) -> RecipeOperationView:
        existing = self._idempotent(request_id, "recipe.build.v1", build_input_sha256)
        if existing is not None:
            if existing.state != "succeeded":
                with self._sessions() as session:
                    succeeded = self._successful_build_job_in_session(
                        session, existing.owner_id, build_input_sha256
                    )
                    if succeeded is not None:
                        return self._view(succeeded)
            return existing
        if build_input_sha256 != plan.build_input_sha256:
            raise RecipeOperationConflict(
                "submitted build input does not match preview"
            )
        now = self._clock()
        with self._sessions.begin() as session:
            build = session.get(RecipeBuild, plan.build_id, with_for_update=True)
            if (
                build is not None
                and build.state == "succeeded"
                and build.build_input_sha256 == plan.build_input_sha256
                and build.builder_node_id == plan.builder_node_id
            ):
                succeeded = self._successful_build_job_in_session(
                    session, build.id, build.build_input_sha256
                )
                if succeeded is None:
                    raise RecipeOperationConflict("recipe build receipt is unavailable")
                replay = Job(
                    id=str(uuid.uuid4()),
                    request_id=request_id,
                    kind=succeeded.kind,
                    state="succeeded",
                    actor=actor,
                    authority_revision=succeeded.authority_revision,
                    targets=list(succeeded.targets),
                    payload_digest=succeeded.payload_digest,
                    payload=dict(succeeded.payload),
                    result=(
                        dict(succeeded.result)
                        if isinstance(succeeded.result, Mapping)
                        else None
                    ),
                    created_at=now,
                    updated_at=now,
                )
                session.add(replay)
                session.flush()
                return self._view(replay)
            if (
                build is None
                or build.build_input_sha256 != plan.build_input_sha256
                or build.builder_node_id != plan.builder_node_id
            ):
                raise RecipeOperationConflict("recipe build preview is stale")
            if build.state == "failed":
                previous = session.scalar(
                    select(Job)
                    .where(
                        Job.kind == "recipe.build.v1",
                        Job.state.in_(("failed", "waiting-for-operator", "expired")),
                        Job.payload["owner_id"].as_string() == build.id,
                        Job.payload["plan_digest"].as_string()
                        == build.build_input_sha256,
                    )
                    .order_by(Job.updated_at.desc())
                    .limit(1)
                )
                if previous is None:
                    raise RecipeOperationConflict(
                        "failed recipe build receipt is unavailable"
                    )
                job = self._retry_build_in_session(
                    session,
                    previous,
                    actor=actor,
                    request_id=request_id,
                    now=now,
                )
            else:
                if build.state != "planned":
                    raise RecipeOperationConflict("recipe build preview is stale")
                if self._builds is None:
                    raise RecipeOperationConflict("recipe build service is unavailable")
                self._builds.reserve_in_session(session, plan, now=now)
                build.state = "building"
                build.updated_at = now
                job = self._queue_in_session(
                    session,
                    kind="recipe.build.v1",
                    owner_kind="recipe-build",
                    owner_id=build.id,
                    plan_digest=plan.build_input_sha256,
                    actor=actor,
                    request_id=request_id,
                    node_payloads=((plan.builder_node_id, plan.agent_payload),),
                    authority_digest=plan.build_input_sha256,
                    now=now,
                )
        self._agent_jobs.notify_available()
        return self.get(job.id)

    @staticmethod
    def _successful_build_job_in_session(
        session: Session, build_id: str, build_input_sha256: str
    ) -> Job | None:
        job = session.scalar(
            select(Job)
            .where(
                Job.kind == "recipe.build.v1",
                Job.state == "succeeded",
                Job.payload["owner_id"].as_string() == build_id,
                Job.payload["plan_digest"].as_string() == build_input_sha256,
            )
            .order_by(Job.updated_at.desc())
            .limit(1)
        )
        return job if job is not None and isinstance(job.result, Mapping) else None

    def preview_install(self, mapping_id: str, recipe_build_id: str) -> InstallPlan:
        return self._install_admission.plan_install(
            mapping_id, recipe_build_id, now=self._clock()
        )

    def preview_image_distribution(
        self,
        recipe_build_id: str,
        mapping_id: str,
        *,
        mapping_generation: int,
    ) -> ImageDistributionPreview:
        if self._builds is None:
            raise RecipeOperationConflict("recipe build service is unavailable")
        try:
            plan = self._builds.plan_distribution(
                recipe_build_id, mapping_id, generation=mapping_generation
            )
        except (KeyError, RuntimeError, ValueError) as error:
            raise RecipeOperationConflict(str(error)) from error
        identity = {
            "schema_version": 1,
            "build_id": plan.build_id,
            "mapping_id": plan.mapping_id,
            "mapping_generation": plan.mapping_generation,
            "image_digest": plan.image_digest,
            "targets": [node_id for node_id, _payload in plan.targets],
        }
        return ImageDistributionPreview(
            recipe_build_id=plan.build_id,
            mapping_id=plan.mapping_id,
            mapping_generation=plan.mapping_generation,
            image_digest=plan.image_digest,
            node_ids=tuple(node_id for node_id, _payload in plan.targets),
            plan_digest=hashlib.sha256(canonical_message(identity)).hexdigest(),
        )

    def distribute_image(
        self,
        recipe_build_id: str,
        mapping_id: str,
        *,
        mapping_generation: int,
        plan_digest: str,
        actor: str,
        request_id: str,
    ) -> RecipeOperationView:
        if self._builds is None:
            raise RecipeOperationConflict("recipe build service is unavailable")
        try:
            plan = self._builds.plan_distribution(
                recipe_build_id, mapping_id, generation=mapping_generation
            )
        except (KeyError, RuntimeError, ValueError) as error:
            raise RecipeOperationConflict(str(error)) from error
        identity = {
            "schema_version": 1,
            "build_id": plan.build_id,
            "mapping_id": plan.mapping_id,
            "mapping_generation": plan.mapping_generation,
            "image_digest": plan.image_digest,
            "targets": [node_id for node_id, _payload in plan.targets],
        }
        actual_plan_digest = hashlib.sha256(canonical_message(identity)).hexdigest()
        if plan_digest != actual_plan_digest:
            raise RecipeOperationConflict(
                "submitted image distribution plan does not match preview"
            )
        existing = self._idempotent(request_id, "recipe.image.import.v1", plan_digest)
        if existing is not None:
            return existing
        if not plan.targets:
            raise RecipeOperationConflict(
                "exact built image is already present on every mapped GPU node"
            )
        return self._queue(
            kind="recipe.image.import.v1",
            owner_kind="image-distribution",
            owner_id=plan.build_id,
            plan_digest=plan_digest,
            actor=actor,
            request_id=request_id,
            node_payloads=plan.targets,
            authority_digest=plan_digest,
        )

    def preview_run(self, installation_id: str, alias: str) -> RunPlan:
        return self._run_admission.plan_run(installation_id, alias, now=self._clock())

    def replay_start(
        self,
        installation_id: str,
        alias: str,
        *,
        plan_digest: str,
        request_id: str,
    ) -> RecipeOperationView | None:
        with self._sessions() as session:
            existing = session.scalar(select(Job).where(Job.request_id == request_id))
            if (
                existing is None
                or existing.kind != "recipe.start"
                or existing.payload.get("owner_kind") != "run"
                or existing.payload.get("plan_digest") != plan_digest
            ):
                return None
            owner_id = existing.payload.get("owner_id")
            if not isinstance(owner_id, str):
                return None
            run = session.get(RecipeRun, owner_id)
            if (
                run is None
                or run.installation_id != installation_id
                or run.alias != alias
                or run.plan_digest != plan_digest
            ):
                return None
            return self._view(existing)

    def run_status(self, run_id: str) -> RecipeRunStatus:
        now = _aware(self._clock())
        with self._sessions() as session:
            run = session.get(RecipeRun, run_id)
            if run is None:
                raise KeyError(run_id)
            nodes = tuple(
                session.scalars(
                    select(RunNode)
                    .where(RunNode.run_id == run_id)
                    .order_by(RunNode.rank)
                )
            )
            expected = run.plan.get("nodes") if isinstance(run.plan, dict) else None
            exact_ranks = (
                isinstance(expected, list)
                and len(expected) == len(nodes)
                and all(isinstance(item, Mapping) for item in expected)
                and {
                    (item.get("node_id"), item.get("rank"), item.get("role"))
                    for item in expected
                }
                == {(node.node_id, node.rank, node.role) for node in nodes}
            )
            ranks: list[RecipeRunRankStatus] = []
            for node in nodes:
                observed_at = _aware(node.updated_at)
                age = now - observed_at
                fresh = timedelta(0) <= age < self._run_health_maximum_age
                ranks.append(
                    RecipeRunRankStatus(
                        node_id=node.node_id,
                        rank=node.rank,
                        role=node.role,
                        state=node.state,
                        observed_at=observed_at,
                        age_seconds=max(0.0, age.total_seconds()),
                        fresh=fresh,
                    )
                )
            return RecipeRunStatus(
                id=run.id,
                alias=run.alias,
                state=run.state,
                route_state=run.route_state,
                healthy=bool(exact_ranks)
                and all(rank.state == "running" and rank.fresh for rank in ranks),
                ranks=tuple(ranks),
            )

    def install(
        self,
        plan: InstallPlan,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
    ) -> RecipeOperationView:
        existing = self._idempotent(request_id, "recipe.install", plan_digest)
        if existing is not None:
            return existing
        if plan_digest != plan.plan_digest:
            raise RecipeOperationConflict(
                "submitted plan digest does not match preview"
            )
        now = self._clock()
        with self._sessions.begin() as session:
            try:
                installation_id = self._install_admission.accept_install_in_session(
                    session, plan, actor=actor, now=now
                )
            except (RuntimeError, ValueError) as error:
                raise RecipeOperationConflict(str(error)) from error
            installation = session.get(RecipeInstallation, installation_id)
            assert installation is not None
            installation.state = "installing"
            installation.updated_at = now
            job = self._queue_in_session(
                session,
                kind="recipe.install",
                owner_kind="installation",
                owner_id=installation_id,
                plan_digest=plan.plan_digest,
                actor=actor,
                request_id=request_id,
                node_payloads=tuple(
                    (
                        node.node_id,
                        {
                            "schema_version": 1,
                            "installation_id": installation_id,
                            "recipe_revision_id": plan.recipe_revision_id,
                            "recipe_content_sha256": plan.recipe_content_sha256,
                            "mapping_id": plan.mapping_id,
                            "mapping_generation": plan.mapping_generation,
                            "recipe_build_id": plan.recipe_build_id,
                            "image_digest": plan.image_digest,
                            "plan_digest": plan.plan_digest,
                            "rank": node.rank,
                            "role": node.role,
                            "expected_bytes": node.required_bytes,
                        },
                    )
                    for node in plan.nodes
                ),
                authority_digest=plan.recipe_content_sha256,
                now=now,
            )
        self._agent_jobs.notify_available()
        return self.get(job.id)

    def start(
        self,
        plan: RunPlan,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
    ) -> RecipeOperationView:
        if plan_digest != plan.plan_digest:
            raise RecipeOperationConflict(
                "submitted plan digest does not match preview"
            )
        existing = self._idempotent(request_id, "recipe.start", plan_digest)
        if existing is not None:
            return existing
        now = self._clock()
        with self._sessions.begin() as session:
            presences = {
                node_id: address
                for node_id, address in session.execute(
                    select(
                        AgentPresence.node_id, AgentPresence.management_address
                    ).where(
                        AgentPresence.node_id.in_([node.node_id for node in plan.nodes])
                    )
                )
            }
            if set(presences) != {node.node_id for node in plan.nodes}:
                raise RecipeOperationConflict(
                    "recipe node endpoint evidence is unavailable"
                )
            master = next((node for node in plan.nodes if node.endpoint_owner), None)
            if master is None:
                raise RecipeOperationConflict("recipe run has no endpoint owner")
            world_size = len(plan.nodes)
            master_address = master.fabric_address if world_size > 1 else None
            master_port = master.rendezvous_port if world_size > 1 else None
            if world_size > 1 and (master_address is None or master_port is None):
                raise RecipeOperationConflict(
                    "recipe direct-fabric rendezvous is unavailable"
                )
            installation_fence = session.get(
                RecipeInstallation, plan.installation_id, with_for_update=True
            )
            if installation_fence is None or installation_fence.state != "installed":
                raise RecipeOperationConflict("recipe installation is not runnable")
            active_uninstall = session.scalar(
                select(Job.id)
                .where(
                    Job.kind == "recipe.uninstall",
                    Job.state.in_({"queued", "running"}),
                    Job.payload["owner_kind"].as_string() == "installation",
                    Job.payload["owner_id"].as_string() == plan.installation_id,
                )
                .limit(1)
            )
            if active_uninstall is not None:
                raise RecipeOperationConflict("recipe installation is not runnable")
            try:
                run_id = self._run_admission.accept_run_in_session(
                    session, plan, actor=actor, now=now
                )
            except (RuntimeError, ValueError) as error:
                raise RecipeOperationConflict(str(error)) from error
            run = session.get(RecipeRun, run_id)
            revision = session.get(LocalRecipeRevision, plan.recipe_revision_id)
            installation = session.get(RecipeInstallation, plan.installation_id)
            assert run is not None and revision is not None and installation is not None
            start_order = _topology_order(revision.document, "start_order")
            run.state = "starting"
            run.updated_at = now
            recipe_digest = revision.content_sha256
            assert recipe_digest is not None
            job = self._queue_in_session(
                session,
                kind="recipe.start",
                owner_kind="run",
                owner_id=run_id,
                plan_digest=plan.plan_digest,
                actor=actor,
                request_id=request_id,
                node_payloads=tuple(
                    (
                        node.node_id,
                        {
                            "schema_version": 1,
                            "run_id": run_id,
                            "installation_id": plan.installation_id,
                            "recipe_revision_id": plan.recipe_revision_id,
                            "recipe_content_sha256": recipe_digest,
                            "mapping_id": plan.mapping_id,
                            "mapping_generation": plan.mapping_generation,
                            "image_digest": installation.image_digest,
                            "plan_digest": plan.plan_digest,
                            "alias": plan.alias,
                            "rank": node.rank,
                            "role": node.role,
                            "port": node.port,
                            "reserved_memory_bytes": node.required_memory_bytes,
                            "endpoint_address": (
                                presences[node.node_id]
                                if node.endpoint_owner
                                else node.fabric_address
                            ),
                            "world_size": world_size,
                            "local_address": node.fabric_address
                            if world_size > 1
                            else None,
                            "master_address": master_address,
                            "master_port": master_port,
                        },
                    )
                    for node in plan.nodes
                ),
                phases=_role_phases(
                    start_order,
                    tuple(
                        (
                            node.node_id,
                            {
                                "schema_version": 1,
                                "run_id": run_id,
                                "installation_id": plan.installation_id,
                                "recipe_revision_id": plan.recipe_revision_id,
                                "recipe_content_sha256": recipe_digest,
                                "mapping_id": plan.mapping_id,
                                "mapping_generation": plan.mapping_generation,
                                "image_digest": installation.image_digest,
                                "plan_digest": plan.plan_digest,
                                "alias": plan.alias,
                                "rank": node.rank,
                                "role": node.role,
                                "port": node.port,
                                "reserved_memory_bytes": node.required_memory_bytes,
                                "endpoint_address": (
                                    presences[node.node_id]
                                    if node.endpoint_owner
                                    else node.fabric_address
                                ),
                                "world_size": world_size,
                                "local_address": node.fabric_address
                                if world_size > 1
                                else None,
                                "master_address": master_address,
                                "master_port": master_port,
                            },
                        )
                        for node in plan.nodes
                    ),
                ),
                authority_digest=recipe_digest,
                now=now,
            )
        self._agent_jobs.notify_available()
        return self.get(job.id)

    def preview_stop(self, run_id: str) -> StopPlan:
        with self._sessions() as session:
            return self._stop_plan_in_session(session, run_id, lock=False)

    def stop(
        self,
        run_id: str,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
    ) -> RecipeOperationView:
        existing = self._idempotent(
            request_id,
            "recipe.stop",
            plan_digest,
            owner_kind="run",
            owner_id=run_id,
        )
        if existing is not None:
            return existing
        now = self._clock()
        try:
            transaction = (
                self._route_publications.publication_transaction()
                if self._route_publications is not None
                else route_publication_transaction(self._sessions)
            )
            with transaction as session:
                existing = self._idempotent_in_session(
                    session,
                    request_id,
                    "recipe.stop",
                    plan_digest,
                    owner_kind="run",
                    owner_id=run_id,
                )
                if existing is not None:
                    return existing
                admitted = self._stop_plan_in_session(session, run_id, lock=True)
                if not admitted.allowed or admitted.plan_digest != plan_digest:
                    raise RecipeOperationConflict("stop plan is stale or blocked")
                prepared = (
                    self._route_publications.prepare_withdrawal_in_session(
                        session, frozenset({run_id})
                    )
                    if self._route_publications is not None
                    else None
                )
                run = session.get(RecipeRun, run_id)
                assert run is not None
                revision = session.get(
                    LocalRecipeRevision, admitted.recipe_revision_id
                )
                if revision is None:
                    raise RecipeOperationConflict(
                        "recipe run topology is unavailable"
                    )
                stop_order = _topology_order(revision.document, "stop_order")
                run.state = "stopping"
                run.updated_at = now
                job = self._queue_in_session(
                    session,
                    kind="recipe.stop",
                    owner_kind="run",
                    owner_id=run_id,
                    plan_digest=admitted.plan_digest,
                    actor=actor,
                    request_id=request_id,
                    node_payloads=tuple(
                        (
                            node.node_id,
                            {
                                "schema_version": 1,
                                "run_id": run_id,
                                "plan_digest": admitted.authority_digest,
                            },
                        )
                        for node in admitted.nodes
                    ),
                    phases=_role_phases(
                        stop_order,
                        tuple(
                            (
                                node.node_id,
                                {
                                    "schema_version": 1,
                                    "run_id": run_id,
                                    "plan_digest": admitted.authority_digest,
                                    "role": node.role,
                                },
                            )
                            for node in admitted.nodes
                        ),
                        include_role=False,
                    ),
                    authority_digest=admitted.authority_digest,
                    now=now,
                )
                session.flush()
                if self._route_publications is not None:
                    self._route_publications.withdraw_run_in_session(
                        session, run_id, prepared=prepared
                    )
                else:
                    self._route_withdrawer(run_id)
                    run.route_state = "withdrawn"
                    run.route_error = None
                    run.updated_at = now
        except IntegrityError as error:
            raced = self._idempotent(
                request_id,
                "recipe.stop",
                plan_digest,
                owner_kind="run",
                owner_id=run_id,
            )
            if raced is not None:
                return raced
            raise RecipeOperationConflict(
                "request key was already used differently"
            ) from error
        self._agent_jobs.notify_available()
        return self.get(job.id)

    def preview_uninstall(self, installation_id: str) -> UninstallPlan:
        with self._sessions() as session:
            return self._uninstall_plan_in_session(session, installation_id, lock=False)

    def uninstall(
        self,
        installation_id: str,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
    ) -> RecipeOperationView:
        existing = self._idempotent(
            request_id,
            "recipe.uninstall",
            plan_digest,
            owner_kind="installation",
            owner_id=installation_id,
        )
        if existing is not None:
            return existing
        now = self._clock()
        try:
            with self._sessions.begin() as session:
                installation_fence = session.scalar(
                    select(RecipeInstallation)
                    .where(RecipeInstallation.id == installation_id)
                    .with_for_update(of=RecipeInstallation)
                )
                if installation_fence is None:
                    raise RecipeOperationConflict("recipe installation does not exist")
                existing = self._idempotent_in_session(
                    session,
                    request_id,
                    "recipe.uninstall",
                    plan_digest,
                    owner_kind="installation",
                    owner_id=installation_id,
                )
                if existing is not None:
                    return existing
                plan = self._uninstall_plan_in_session(
                    session, installation_id, lock=True
                )
                if not plan.allowed or plan.plan_digest != plan_digest:
                    raise RecipeOperationConflict("uninstall plan is stale or blocked")
                job = self._queue_in_session(
                    session,
                    kind="recipe.uninstall",
                    owner_kind="installation",
                    owner_id=installation_id,
                    plan_digest=plan.plan_digest,
                    actor=actor,
                    request_id=request_id,
                    node_payloads=tuple(
                        (
                            node.node_id,
                            {
                                "schema_version": 1,
                                "installation_id": installation_id,
                                "recipe_content_sha256": (
                                    plan.installation_authority_digest
                                ),
                                "plan_digest": plan.original_plan_digest,
                            },
                        )
                        for node in plan.nodes
                    ),
                    authority_digest=plan.installation_authority_digest,
                    now=now,
                )
        except IntegrityError as error:
            raced = self._idempotent(
                request_id,
                "recipe.uninstall",
                plan_digest,
                owner_kind="installation",
                owner_id=installation_id,
            )
            if raced is not None:
                return raced
            raise RecipeOperationConflict(
                "request key was already used differently"
            ) from error
        self._agent_jobs.notify_available()
        return self.get(job.id)

    def retry(
        self, operation_id: str, *, actor: str, request_id: str
    ) -> RecipeOperationView:
        now = self._clock()
        with self._sessions.begin() as session:
            previous = session.get(Job, operation_id, with_for_update=True)
            if previous is None:
                raise RecipeOperationConflict("recipe operation is not retryable")
            previous_plan_digest = _required_string(previous.payload, "plan_digest")
            existing = session.scalar(select(Job).where(Job.request_id == request_id))
            if existing is not None:
                if (
                    existing.kind != previous.kind
                    or _required_string(existing.payload, "plan_digest")
                    != previous_plan_digest
                ):
                    raise RecipeOperationConflict("request key was already used")
                return self._view(existing)
            if previous.kind == "recipe.build.v1":
                job = self._retry_build_in_session(
                    session, previous, actor=actor, request_id=request_id, now=now
                )
            elif previous.kind == "recipe.image.import.v1":
                job = self._retry_image_distribution_in_session(
                    session, previous, actor=actor, request_id=request_id, now=now
                )
            elif previous.kind == "recipe.install" and previous.state == "failed":
                job = self._retry_install_in_session(
                    session, previous, actor=actor, request_id=request_id, now=now
                )
            else:
                raise RecipeOperationConflict("recipe operation is not retryable")
        self._agent_jobs.notify_available()
        return self.get(job.id)

    def _retry_image_distribution_in_session(
        self,
        session: Session,
        previous: Job,
        *,
        actor: str,
        request_id: str,
        now: datetime,
    ) -> Job:
        if previous.state not in _RETRYABLE_IMAGE_DISTRIBUTION_STATES:
            raise RecipeOperationConflict("recipe image distribution is not retryable")
        plan_digest = _required_string(previous.payload, "plan_digest")
        owner_id = _required_string(previous.payload, "owner_id")
        active = any(
            job.id != previous.id
            and job.state in {"queued", "running"}
            and isinstance(job.payload, Mapping)
            and job.payload.get("owner_id") == owner_id
            and job.payload.get("plan_digest") == plan_digest
            for job in session.scalars(
                select(Job).where(Job.kind == "recipe.image.import.v1")
            )
        )
        if active:
            raise RecipeOperationConflict(
                "recipe image distribution already has an active retry"
            )
        children = tuple(
            session.scalars(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == previous.id)
                .order_by(AgentOperation.node_id)
            )
        )
        payload_identities = {
            (
                child.payload.get("mapping_id"),
                child.payload.get("mapping_generation"),
                child.payload.get("source_node_id"),
                child.payload.get("image_digest"),
                child.payload.get("oci_layout_sha256"),
                child.payload.get("image_bytes"),
            )
            for child in children
            if isinstance(child.payload, Mapping)
        }
        if (
            previous.payload.get("owner_kind") != "image-distribution"
            or not children
            or tuple(child.node_id for child in children)
            != tuple(sorted(previous.targets))
            or previous.authority_revision != plan_digest.removeprefix("sha256:")
            or len(payload_identities) != 1
            or any(
                child.kind != "recipe.image.import.v1"
                or child.state not in _RETRYABLE_IMAGE_OPERATION_STATES
                or child.authority_revision != plan_digest.removeprefix("sha256:")
                or child.payload_digest
                != hashlib.sha256(canonical_message(child.payload)).hexdigest()
                or not _valid_image_import_payload(child.payload, owner_id)
                for child in children
            )
        ):
            raise RecipeOperationConflict("stored recipe image distribution is invalid")
        return self._queue_in_session(
            session,
            kind="recipe.image.import.v1",
            owner_kind="image-distribution",
            owner_id=owner_id,
            plan_digest=plan_digest,
            actor=actor,
            request_id=request_id,
            node_payloads=tuple(
                (child.node_id, dict(child.payload)) for child in children
            ),
            authority_digest=plan_digest,
            now=now,
        )

    def _retry_install_in_session(
        self,
        session: Session,
        previous: Job,
        *,
        actor: str,
        request_id: str,
        now: datetime,
    ) -> Job:
        previous_plan_digest = _required_string(previous.payload, "plan_digest")
        owner_id = _required_string(previous.payload, "owner_id")
        installation = session.get(RecipeInstallation, owner_id, with_for_update=True)
        if installation is None or installation.state not in {"partial", "failed"}:
            raise RecipeOperationConflict("recipe installation is not retryable")
        recipe_revision_id = installation.recipe_revision_id
        nodes = tuple(
            session.scalars(
                select(InstallationNode)
                .where(InstallationNode.installation_id == installation.id)
                .order_by(InstallationNode.node_id)
            )
        )
        revision = session.get(LocalRecipeRevision, installation.recipe_revision_id)
        assert revision is not None and revision.content_sha256 is not None
        recipe_digest = revision.content_sha256
        installation.state = "installing"
        installation.updated_at = now
        for node in nodes:
            node.state = "planned"
        return self._queue_in_session(
            session,
            kind="recipe.install",
            owner_kind="installation",
            owner_id=owner_id,
            plan_digest=previous_plan_digest,
            actor=actor,
            request_id=request_id,
            node_payloads=tuple(
                (
                    node.node_id,
                    {
                        "schema_version": 1,
                        "installation_id": owner_id,
                        "recipe_revision_id": recipe_revision_id,
                        "recipe_content_sha256": recipe_digest,
                        "mapping_id": installation.mapping_id,
                        "mapping_generation": installation.mapping_generation,
                        "recipe_build_id": installation.recipe_build_id,
                        "image_digest": installation.image_digest,
                        "plan_digest": previous_plan_digest,
                        "rank": node.rank,
                        "role": node.role,
                        "expected_bytes": node.required_bytes,
                    },
                )
                for node in nodes
            ),
            authority_digest=recipe_digest,
            now=now,
        )

    def _retry_build_in_session(
        self,
        session: Session,
        previous: Job,
        *,
        actor: str,
        request_id: str,
        now: datetime,
    ) -> Job:
        if previous.state not in {"failed", "waiting-for-operator", "expired"}:
            raise RecipeOperationConflict("recipe build is not retryable")
        if self._builds is None:
            raise RecipeOperationConflict("recipe build service is unavailable")
        owner_id = _required_string(previous.payload, "owner_id")
        build = session.get(RecipeBuild, owner_id, with_for_update=True)
        if build is None or build.state not in {"building", "failed"}:
            raise RecipeOperationConflict("recipe build is not retryable")
        active = any(
            job.id != previous.id
            and job.state in {"queued", "running"}
            and isinstance(job.payload, Mapping)
            and job.payload.get("owner_id") == owner_id
            for job in session.scalars(select(Job).where(Job.kind == "recipe.build.v1"))
        )
        if active:
            raise RecipeOperationConflict("recipe build already has an active retry")
        payload = dict(build.plan) if isinstance(build.plan, Mapping) else {}
        try:
            plan = RecipeBuildPlan(
                build_id=owner_id,
                recipe_revision_id=_required_string(payload, "recipe_revision_id"),
                recipe_content_sha256=_required_string(
                    payload, "recipe_content_sha256"
                ),
                builder_node_id=build.builder_node_id,
                source_bundle_sha256=_required_string(payload, "source_bundle_sha256"),
                build_input_sha256=build.build_input_sha256,
                agent_payload=payload,
            )
        except (KeyError, TypeError) as error:
            raise RecipeOperationConflict(
                "stored recipe build plan is invalid"
            ) from error
        if (
            payload.get("build_id") != owner_id
            or payload.get("build_input_sha256") != build.build_input_sha256
        ):
            raise RecipeOperationConflict("stored recipe build plan is invalid")
        self._release(session, "recipe-build", owner_id, now)
        self._builds.reserve_in_session(session, plan, now=now)
        build.state = "building"
        build.error = None
        build.updated_at = now
        return self._queue_in_session(
            session,
            kind="recipe.build.v1",
            owner_kind="recipe-build",
            owner_id=owner_id,
            plan_digest=build.build_input_sha256,
            actor=actor,
            request_id=request_id,
            node_payloads=((build.builder_node_id, payload),),
            authority_digest=build.build_input_sha256,
            now=now,
        )

    def record_node_result(
        self,
        operation_id: str,
        node_id: str,
        *,
        succeeded: bool,
        evidence: Mapping[str, object],
    ) -> RecipeOperationView:
        now = self._clock()
        with self._sessions.begin() as session:
            job = session.get(Job, operation_id)
            if job is None or not job.kind.startswith("recipe."):
                raise KeyError(operation_id)
            operation = session.scalar(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == job.id,
                    AgentOperation.node_id == node_id,
                )
            )
            if operation is None:
                raise RecipeOperationConflict("node is not part of operation group")
            operation.state = "succeeded" if succeeded else "failed"
            operation.updated_at = now
            cleanup_queued = self._project_node_result(
                session,
                job,
                operation,
                succeeded=succeeded,
                evidence=evidence,
                now=now,
            )
        if cleanup_queued:
            self._agent_jobs.notify_available()
        return self.get(operation_id)

    def consume_agent_result(
        self,
        session: Session,
        operation: AgentOperation,
        _attempt: object,
        message: object,
    ) -> None:
        """Project an authenticated agent result in the queue transaction."""
        job = session.get(Job, operation.parent_job_id)
        if job is None or not job.kind.startswith("recipe."):
            return
        state = getattr(message, "state", None)
        result = getattr(message, "result", None)
        if state not in {"succeeded", "failed"} or not isinstance(result, Mapping):
            raise RecipeOperationConflict("recipe agent result is invalid")
        raw_evidence = result.get("evidence", result)
        if not isinstance(raw_evidence, Mapping):
            raise RecipeOperationConflict("recipe agent evidence is invalid")
        if raw_evidence is not result and "evidence_digest" in result:
            raw_evidence = {
                **raw_evidence,
                "evidence_digest": result["evidence_digest"],
            }
        self._project_node_result(
            session,
            job,
            operation,
            succeeded=state == "succeeded",
            evidence=raw_evidence,
            now=self._clock(),
        )

    def _project_node_result(
        self,
        session: Session,
        job: Job,
        operation: AgentOperation,
        *,
        succeeded: bool,
        evidence: Mapping[str, object],
        now: datetime,
    ) -> bool:
        cleanup_queued = False
        locked_job = session.get(Job, job.id, with_for_update=True)
        if locked_job is None:
            raise RecipeOperationConflict("recipe operation disappeared")
        job = locked_job
        # Lock acquisition can outlive a retained recovery deadline. Every
        # phase/terminal transition uses a fresh time sampled inside the lock.
        now = self._clock()
        operation.updated_at = now
        node_id = operation.node_id
        owner_id = _required_string(job.payload, "owner_id")
        if job.kind == "recipe.build.v1":
            build = session.get(RecipeBuild, owner_id, with_for_update=True)
            if build is None or build.builder_node_id != node_id:
                raise RecipeOperationConflict("recipe build authority is invalid")
            if succeeded:
                _record_build_evidence(session, build, evidence, now=now)
            else:
                build.state = "failed"
                build.error = str(evidence.get("reason", "agent build failed"))[:512]
                build.updated_at = now
        elif job.kind == "recipe.image.import.v1":
            _record_image_import_evidence(session, operation, evidence, succeeded, now)
        elif job.kind == "recipe.install":
            node = session.scalar(
                select(InstallationNode).where(
                    InstallationNode.installation_id == owner_id,
                    InstallationNode.node_id == node_id,
                )
            )
            assert node is not None
            node.state = "installed" if succeeded else "failed"
            if succeeded:
                installed_bytes = evidence.get("installed_bytes")
                if not isinstance(installed_bytes, int) or installed_bytes < 0:
                    raise RecipeOperationConflict("install evidence is invalid")
                node.installed_bytes = installed_bytes
            node.updated_at = now
        elif job.kind in {"recipe.start", "recipe.stop"}:
            node = session.scalar(
                select(RunNode).where(
                    RunNode.run_id == owner_id, RunNode.node_id == node_id
                )
            )
            assert node is not None
            node.state = (
                "running"
                if job.kind == "recipe.start" and succeeded
                else "stopped"
                if succeeded
                else "failed"
            )
            if job.kind == "recipe.start" and succeeded:
                endpoint, digest = _validate_start_evidence(
                    session, owner_id, operation, evidence
                )
                node.endpoint = {"url": endpoint}
                node.evidence_digest = digest
            node.updated_at = now
        elif job.kind == "recipe.uninstall":
            node = session.scalar(
                select(InstallationNode).where(
                    InstallationNode.installation_id == owner_id,
                    InstallationNode.node_id == node_id,
                )
            )
            assert node is not None
            node.state = "uninstalled" if succeeded else "failed"
            node.updated_at = now
        recorded_result = dict(job.result) if isinstance(job.result, Mapping) else {}
        raw_node_evidence = recorded_result.get("node_evidence", {})
        if not isinstance(raw_node_evidence, Mapping):
            raise RecipeOperationConflict("recipe operation evidence is invalid")
        node_evidence = {
            str(recorded_node): dict(recorded_evidence)
            for recorded_node, recorded_evidence in raw_node_evidence.items()
            if isinstance(recorded_node, str) and isinstance(recorded_evidence, Mapping)
        }
        if len(node_evidence) != len(raw_node_evidence):
            raise RecipeOperationConflict("recipe operation evidence is invalid")
        observed_evidence = json.loads(canonical_message(evidence))
        if node_id in node_evidence and node_evidence[node_id] != observed_evidence:
            raise RecipeOperationConflict("recipe node evidence changed")
        node_evidence[node_id] = observed_evidence
        job.result = {**recorded_result, "node_evidence": node_evidence}
        children = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
            )
        )
        phases = _stored_phases(job.payload)
        recovery_error: DistributedLifecycleError | None = None
        if phases:
            phase_index = _current_phase_index(children, phases)
            if phase_index is not None:
                phase_nodes = {node_id for node_id, _payload in phases[phase_index]}
                phase_children = tuple(
                    child for child in children if child.node_id in phase_nodes
                )
                if any(
                    child.state not in _TERMINAL_JOB_STATES for child in phase_children
                ):
                    job.state = "running"
                    job.updated_at = now
                    return cleanup_queued
                if all(child.state == "succeeded" for child in phase_children) and (
                    phase_index + 1 < len(phases)
                ):
                    try:
                        enforce_recovery_deadline(job.payload, now=now)
                    except DistributedLifecycleError as error:
                        recovery_error = error
                    if recovery_error is None:
                        for next_node_id, next_payload in phases[phase_index + 1]:
                            self._agent_jobs.enqueue_in_session(
                                session,
                                job.id,
                                next_node_id,
                                job.kind,
                                job.authority_revision,
                                next_payload,
                                operation_id=str(uuid.uuid4()),
                            )
                        job.state = "running"
                        job.updated_at = now
                        return True
        terminal = all(child.state in _TERMINAL_JOB_STATES for child in children)
        if terminal:
            successful = sorted(
                child.node_id for child in children if child.state == "succeeded"
            )
            failed = sorted(
                child.node_id for child in children if child.state == "failed"
            )
            if job.kind == "recipe.start" and recovery_error is None:
                try:
                    enforce_recovery_deadline(job.payload, now=now)
                except DistributedLifecycleError as error:
                    recovery_error = error
            start_failed = bool(failed) or recovery_error is not None
            job.state = "failed" if start_failed else "succeeded"
            job.result = {
                "successful_nodes": successful,
                "failed_nodes": failed,
                "node_evidence": node_evidence,
            }
            if recovery_error is not None:
                job.result = {
                    **job.result,
                    "recovery_error": str(recovery_error),
                }
            if job.kind == "recipe.build.v1":
                self._release(session, "recipe-build", owner_id, now)
            elif job.kind == "recipe.install":
                installation = session.get(RecipeInstallation, owner_id)
                assert installation is not None
                installation.state = "partial" if failed else "installed"
                installation.updated_at = now
            elif job.kind == "recipe.start":
                run = session.get(RecipeRun, owner_id)
                assert run is not None
                if start_failed:
                    run.state = "stopping"
                    run.route_state = "withdrawn"
                    run.route_error = (
                        f"{recovery_error}; cleanup queued"
                        if recovery_error is not None
                        else "one or more ranks failed to start; cleanup queued"
                    )
                    cleanup_request_id = str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"vonk:recipe-start-cleanup:{job.id}",
                        )
                    )
                    if not session.scalar(
                        select(Job.id).where(Job.request_id == cleanup_request_id)
                    ):
                        stop_nodes = tuple(
                            session.scalars(
                                select(RunNode)
                                .where(RunNode.run_id == owner_id)
                                .order_by(RunNode.rank)
                            )
                        )
                        installation = session.get(
                            RecipeInstallation, run.installation_id
                        )
                        revision = (
                            session.get(
                                LocalRecipeRevision, installation.recipe_revision_id
                            )
                            if installation is not None
                            else None
                        )
                        if revision is None:
                            raise RecipeOperationConflict(
                                "recipe run topology is unavailable"
                            )
                        self._queue_in_session(
                            session,
                            kind="recipe.stop",
                            owner_kind="run",
                            owner_id=owner_id,
                            plan_digest=run.plan_digest,
                            actor=job.actor,
                            request_id=cleanup_request_id,
                            node_payloads=tuple(
                                (
                                    run_node.node_id,
                                    {
                                        "schema_version": 1,
                                        "run_id": owner_id,
                                        "plan_digest": run.plan_digest,
                                    },
                                )
                                for run_node in stop_nodes
                            ),
                            phases=_role_phases(
                                _topology_order(revision.document, "stop_order"),
                                tuple(
                                    (
                                        run_node.node_id,
                                        {
                                            "schema_version": 1,
                                            "run_id": owner_id,
                                            "plan_digest": run.plan_digest,
                                            "role": run_node.role,
                                        },
                                    )
                                    for run_node in stop_nodes
                                ),
                                include_role=False,
                            ),
                            authority_digest=run.plan_digest,
                            now=now,
                        )
                        cleanup_queued = True
                else:
                    run.state = "running"
                    run.route_state = "pending"
                    run.route_error = None
                run.updated_at = now
            elif job.kind == "recipe.stop":
                run = session.get(RecipeRun, owner_id)
                assert run is not None
                recovery = None
                recovery_error: DistributedLifecycleError | None = None
                try:
                    recovery = recovery_start_plan(job.payload, now=now)
                except DistributedLifecycleError as error:
                    recovery_error = error
                if recovery is not None and not failed:
                    phases, marker = recovery
                    installation = session.get(
                        RecipeInstallation, run.installation_id
                    )
                    revision = (
                        session.get(
                            LocalRecipeRevision, installation.recipe_revision_id
                        )
                        if installation is not None
                        else None
                    )
                    if revision is None or revision.content_sha256 is None:
                        raise RecipeOperationConflict(
                            "distributed recovery authority is unavailable"
                        )
                    flattened = tuple(item for phase in phases for item in phase)
                    self._queue_in_session(
                        session,
                        kind="recipe.start",
                        owner_kind="run",
                        owner_id=owner_id,
                        plan_digest=run.plan_digest,
                        actor=job.actor,
                        request_id=str(
                            uuid.uuid5(
                                uuid.NAMESPACE_URL,
                                f"vonk:distributed-recovery-start:{job.id}",
                            )
                        ),
                        node_payloads=flattened,
                        phases=phases,
                        authority_digest=revision.content_sha256,
                        now=now,
                        job_context={"recovery": marker},
                    )
                    run.state = "starting"
                    run.route_state = "withdrawn"
                    run.route_error = "distributed recovery restarting"
                    run.updated_at = now
                    cleanup_queued = True
                else:
                    run.state = "failed" if failed or recovery_error else "stopped"
                    run.stopped_at = now if not failed else None
                    run.route_state = "withdrawn"
                    if recovery_error is not None:
                        run.route_error = str(recovery_error)[:512]
                    run.updated_at = now
                    if not failed:
                        self._release(session, "run", owner_id, now)
            elif job.kind == "recipe.uninstall":
                installation = session.get(RecipeInstallation, owner_id)
                assert installation is not None
                installation.state = "failed" if failed else "uninstalled"
                installation.updated_at = now
                if not failed:
                    self._release(session, "installation", owner_id, now)
        else:
            job.state = "running"
        job.updated_at = now
        return cleanup_queued

    def get(self, operation_id: str) -> RecipeOperationView:
        with self._sessions() as session:
            job = session.get(Job, operation_id)
            if job is None or not job.kind.startswith("recipe."):
                raise KeyError(operation_id)
            return self._view(job)

    def cancel(
        self, operation_id: str, *, actor: str, request_id: str, reason: str
    ) -> RecipeOperationView:
        """Durably cancel a queued/running recipe operation."""
        now = self._clock()
        cancellation_reason = _cancel_reason(reason)
        with self._sessions.begin() as session:
            job = session.get(Job, operation_id, with_for_update=True)
            if job is None or not job.kind.startswith("recipe."):
                raise RecipeOperationConflict("recipe operation is not cancellable")
            if job.state == "cancelled":
                return self._view(job)
            if job.state not in {"queued", "running"}:
                raise RecipeOperationConflict("recipe operation is not cancellable")
            children = tuple(session.scalars(select(AgentOperation).where(AgentOperation.parent_job_id == job.id)))
            for child in children:
                if child.state not in _TERMINAL_JOB_STATES:
                    child.state = "cancelled"
                    child.updated_at = now
            job.state = "cancelled"
            job.status_reason = cancellation_reason
            job.result = {**(dict(job.result) if isinstance(job.result, Mapping) else {}), "cancelled": True, "reason": cancellation_reason, "recovery": "retry creates a new operation"}
            job.updated_at = now
        return self.get(operation_id)

    def _stop_plan_in_session(
        self, session: Session, run_id: str, *, lock: bool
    ) -> StopPlan:
        run_statement = select(RecipeRun).where(RecipeRun.id == run_id)
        if lock:
            run_statement = run_statement.with_for_update(of=RecipeRun)
        run = session.scalar(run_statement)
        if run is None:
            raise RecipeOperationConflict("recipe run does not exist")

        installation_statement = select(RecipeInstallation).where(
            RecipeInstallation.id == run.installation_id
        )
        if lock:
            installation_statement = installation_statement.with_for_update(
                of=RecipeInstallation
            )
        installation = session.scalar(installation_statement)
        if installation is None:
            raise RecipeOperationConflict("recipe installation does not exist")

        revision_statement = select(LocalRecipeRevision).where(
            LocalRecipeRevision.id == installation.recipe_revision_id
        )
        if lock:
            revision_statement = revision_statement.with_for_update(
                of=LocalRecipeRevision
            )
        revision = session.scalar(revision_statement)
        if revision is None:
            raise RecipeOperationConflict("recipe revision does not exist")

        node_statement = (
            select(RunNode)
            .where(RunNode.run_id == run_id)
            .order_by(RunNode.rank, RunNode.node_id)
            .limit(_MAX_ACTION_NODES + 1)
        )
        if lock:
            node_statement = node_statement.with_for_update(of=RunNode)
        all_nodes = tuple(session.scalars(node_statement))
        nodes = all_nodes[:_MAX_ACTION_NODES]

        reservation_statement = (
            select(ResourceReservation)
            .where(
                ResourceReservation.owner_kind == "run",
                ResourceReservation.owner_id == run_id,
                ResourceReservation.kind.in_(_MEMORY_RESERVATION_KINDS),
                ResourceReservation.state == "active",
            )
            .order_by(
                ResourceReservation.node_id,
                ResourceReservation.kind,
                ResourceReservation.resource_key,
                ResourceReservation.id,
            )
        )
        if lock:
            reservation_statement = reservation_statement.with_for_update(
                of=ResourceReservation
            )
        reservations = tuple(session.scalars(reservation_statement))
        active_by_node = {node.node_id: 0 for node in nodes}
        for reservation in reservations:
            if reservation.node_id in active_by_node:
                active_by_node[reservation.node_id] += reservation.amount_bytes

        expected_nodes = (
            run.plan.get("nodes") if isinstance(run.plan, Mapping) else None
        )
        expected_identity = (
            {
                (item.get("node_id"), item.get("rank"), item.get("role"))
                for item in expected_nodes
            }
            if isinstance(expected_nodes, list)
            and all(isinstance(item, Mapping) for item in expected_nodes)
            else set()
        )
        actual_identity = {(node.node_id, node.rank, node.role) for node in nodes}
        immutable_membership_exact = (
            len(all_nodes) <= _MAX_ACTION_NODES
            and isinstance(expected_nodes, list)
            and len(expected_nodes) == len(nodes)
            and len(expected_identity) == len(nodes)
            and expected_identity == actual_identity
            and run.plan.get("installation_id") == run.installation_id
            and run.plan.get("mapping_id") == run.mapping_id
            and run.plan.get("mapping_generation") == run.mapping_generation
            and run.plan.get("recipe_revision_id") == revision.id
            and run.plan.get("plan_digest") == run.plan_digest
        )
        reservation_membership_exact = (
            len(reservations) == len(nodes)
            and {reservation.node_id for reservation in reservations}
            == {node.node_id for node in nodes}
            and all(
                reservation.plan_digest == run.plan_digest
                for reservation in reservations
            )
            and all(
                active_by_node[node.node_id] == node.reserved_memory_bytes
                for node in nodes
            )
        )
        reservation_facts = tuple(
            {
                "id": reservation.id,
                "node_id": reservation.node_id,
                "kind": reservation.kind,
                "resource_key": reservation.resource_key,
                "amount_bytes": reservation.amount_bytes,
                "plan_digest": reservation.plan_digest,
                "state": reservation.state,
            }
            for reservation in reservations
        )
        return stop_plan(
            run_id=run.id,
            installation_id=run.installation_id,
            recipe_revision_id=revision.id,
            alias=run.alias,
            run_state=run.state,
            route_state=run.route_state,
            route_generation=run.route_generation,
            route_digest=run.route_digest,
            authority_digest=run.plan_digest,
            nodes=tuple(
                StopNodeImpact(
                    node_id=node.node_id,
                    rank=node.rank,
                    role=node.role,
                    state=node.state,
                    reserved_memory_bytes=node.reserved_memory_bytes,
                    active_memory_reservation_bytes=active_by_node[node.node_id],
                )
                for node in nodes
            ),
            immutable_membership_exact=immutable_membership_exact,
            reservation_membership_exact=reservation_membership_exact,
            reservation_facts=reservation_facts,
        )

    def _uninstall_plan_in_session(
        self, session: Session, installation_id: str, *, lock: bool
    ) -> UninstallPlan:
        installation_statement = select(RecipeInstallation).where(
            RecipeInstallation.id == installation_id
        )
        if lock:
            installation_statement = installation_statement.with_for_update(
                of=RecipeInstallation
            )
        installation = session.scalar(installation_statement)
        if installation is None:
            raise RecipeOperationConflict("recipe installation does not exist")

        revision_statement = select(LocalRecipeRevision).where(
            LocalRecipeRevision.id == installation.recipe_revision_id
        )
        if lock:
            revision_statement = revision_statement.with_for_update(
                of=LocalRecipeRevision
            )
        revision = session.scalar(revision_statement)
        if (
            revision is None
            or revision.content_sha256 is None
            or not isinstance(revision.document, Mapping)
        ):
            raise RecipeOperationConflict("recipe revision authority is unavailable")

        node_statement = (
            select(InstallationNode)
            .where(InstallationNode.installation_id == installation_id)
            .order_by(InstallationNode.rank, InstallationNode.node_id)
            .limit(_MAX_ACTION_NODES + 1)
        )
        if lock:
            node_statement = node_statement.with_for_update(of=InstallationNode)
        all_nodes = tuple(session.scalars(node_statement))
        nodes = all_nodes[:_MAX_ACTION_NODES]

        active_count = int(
            session.scalar(
                select(func.count(RecipeRun.id)).where(
                    RecipeRun.installation_id == installation_id,
                    RecipeRun.state != "stopped",
                )
            )
            or 0
        )
        active_statement = (
            select(RecipeRun)
            .where(
                RecipeRun.installation_id == installation_id,
                RecipeRun.state != "stopped",
            )
            .order_by(RecipeRun.id)
            .limit(_MAX_ACTIVE_RUNS)
        )
        if lock:
            active_statement = active_statement.with_for_update(of=RecipeRun)
        active_runs = tuple(session.scalars(active_statement))

        operation_statement = (
            select(Job)
            .where(
                Job.kind == "recipe.uninstall",
                Job.state.in_({"queued", "running"}),
                Job.payload["owner_id"].as_string() == installation_id,
            )
            .order_by(Job.id)
            .limit(1)
        )
        if lock:
            operation_statement = operation_statement.with_for_update(of=Job)
        active_operation = session.scalar(operation_statement) is not None

        expected_nodes = (
            installation.plan.get("nodes")
            if isinstance(installation.plan, Mapping)
            else None
        )
        expected_identity = (
            {
                (item.get("node_id"), item.get("rank"), item.get("role"))
                for item in expected_nodes
            }
            if isinstance(expected_nodes, list)
            and all(isinstance(item, Mapping) for item in expected_nodes)
            else set()
        )
        actual_identity = {(node.node_id, node.rank, node.role) for node in nodes}
        immutable_membership_exact = (
            len(all_nodes) <= _MAX_ACTION_NODES
            and isinstance(expected_nodes, list)
            and len(expected_nodes) == len(nodes)
            and len(expected_identity) == len(nodes)
            and expected_identity == actual_identity
            and installation.plan.get("mapping_id") == installation.mapping_id
            and installation.plan.get("mapping_generation")
            == installation.mapping_generation
            and installation.plan.get("recipe_revision_id") == revision.id
            and installation.plan.get("recipe_content_sha256")
            == revision.content_sha256
            and installation.plan.get("plan_digest") == installation.plan_digest
        )
        return uninstall_plan(
            installation_id=installation.id,
            recipe_id=revision.recipe_id,
            recipe_revision_id=revision.id,
            recipe_content_sha256=revision.content_sha256,
            recipe_content=revision.document,
            original_plan_digest=installation.plan_digest,
            installation_state=installation.state,
            nodes=tuple(
                UninstallNodeImpact(
                    node_id=node.node_id,
                    rank=node.rank,
                    role=node.role,
                    state=node.state,
                    installed_bytes=(
                        node.installed_bytes if node.state == "installed" else None
                    ),
                )
                for node in nodes
            ),
            immutable_membership_exact=immutable_membership_exact,
            active_runs=tuple(
                UninstallActiveRun(
                    run_id=run.id,
                    alias=run.alias,
                    state=run.state,
                    route_state=run.route_state,
                )
                for run in active_runs
            ),
            active_run_count=active_count,
            active_runs_truncated=active_count > _MAX_ACTIVE_RUNS,
            active_operation=active_operation,
        )

    def _idempotent(
        self,
        request_id: str,
        kind: str,
        plan_digest: str | None,
        *,
        owner_kind: str | None = None,
        owner_id: str | None = None,
    ) -> RecipeOperationView | None:
        with self._sessions() as session:
            return self._idempotent_in_session(
                session,
                request_id,
                kind,
                plan_digest,
                owner_kind=owner_kind,
                owner_id=owner_id,
            )

    def _idempotent_in_session(
        self,
        session: Session,
        request_id: str,
        kind: str,
        plan_digest: str | None,
        *,
        owner_kind: str | None = None,
        owner_id: str | None = None,
    ) -> RecipeOperationView | None:
        existing = session.scalar(select(Job).where(Job.request_id == request_id))
        if existing is None:
            return None
        existing_digest = existing.payload.get("plan_digest")
        if (
            existing.kind != kind
            or (plan_digest is not None and existing_digest != plan_digest)
            or (
                owner_kind is not None
                and existing.payload.get("owner_kind") != owner_kind
            )
            or (owner_id is not None and existing.payload.get("owner_id") != owner_id)
        ):
            raise RecipeOperationConflict("request key was already used differently")
        return self._view(existing)

    def _queue(
        self,
        *,
        kind: str,
        owner_kind: str,
        owner_id: str,
        plan_digest: str,
        actor: str,
        request_id: str,
        node_payloads: Sequence[tuple[str, Mapping[str, object]]],
        authority_digest: str,
    ) -> RecipeOperationView:
        now = self._clock()
        with self._sessions.begin() as session:
            job = self._queue_in_session(
                session,
                kind=kind,
                owner_kind=owner_kind,
                owner_id=owner_id,
                plan_digest=plan_digest,
                actor=actor,
                request_id=request_id,
                node_payloads=node_payloads,
                authority_digest=authority_digest,
                now=now,
            )
        self._agent_jobs.notify_available()
        return self.get(job.id)

    def _queue_in_session(
        self,
        session: Session,
        *,
        kind: str,
        owner_kind: str,
        owner_id: str,
        plan_digest: str,
        actor: str,
        request_id: str,
        node_payloads: Sequence[tuple[str, Mapping[str, object]]],
        authority_digest: str,
        now: datetime,
        phases: Sequence[Sequence[tuple[str, Mapping[str, object]]]] | None = None,
        job_context: Mapping[str, object] | None = None,
    ) -> Job:
        if not node_payloads:
            raise RecipeOperationConflict("operation group has no target nodes")
        if session.scalar(select(Job.id).where(Job.request_id == request_id)):
            raise RecipeOperationConflict("request key was already used differently")
        job_id = str(uuid.uuid4())
        phase_groups = (
            tuple(tuple(group) for group in phases)
            if phases is not None
            else (tuple(node_payloads),)
        )
        if not phase_groups or any(not group for group in phase_groups):
            raise RecipeOperationConflict("operation phases are invalid")
        flattened = tuple(item for group in phase_groups for item in group)
        if {node_id for node_id, _payload in flattened} != {
            node_id for node_id, _payload in node_payloads
        } or len({node_id for node_id, _payload in flattened}) != len(flattened):
            raise RecipeOperationConflict("operation phases do not match target nodes")
        targets = sorted(node_id for node_id, _payload in flattened)
        job_payload: dict[str, object] = {
            "schema_version": 1,
            "owner_kind": owner_kind,
            "owner_id": owner_id,
            "plan_digest": plan_digest,
        }
        if phases is not None:
            job_payload["phases"] = [
                [
                    {"node_id": node_id, "payload": dict(payload)}
                    for node_id, payload in group
                ]
                for group in phase_groups
            ]
        if job_context is not None:
            if set(job_context) & set(job_payload):
                raise RecipeOperationConflict("operation context is invalid")
            job_payload.update(json.loads(canonical_message(job_context)))
        job = Job(
            id=job_id,
            request_id=request_id,
            kind=kind,
            state="running",
            actor=actor,
            authority_revision=authority_digest.removeprefix("sha256:"),
            targets=targets,
            payload_digest=hashlib.sha256(canonical_message(job_payload)).hexdigest(),
            payload=job_payload,
            created_at=now,
            updated_at=now,
        )
        session.add(job)
        session.flush()
        for node_id, payload in phase_groups[0]:
            self._agent_jobs.enqueue_in_session(
                session,
                job_id,
                node_id,
                kind,
                authority_digest.removeprefix("sha256:"),
                payload,
                operation_id=str(uuid.uuid4()),
            )
        return job

    def _view(self, job: Job) -> RecipeOperationView:
        return RecipeOperationView(
            id=job.id,
            kind=job.kind,
            owner_id=_required_string(job.payload, "owner_id"),
            state=job.state,
            plan_digest=_required_string(job.payload, "plan_digest"),
            nodes=tuple(job.targets),
            result=dict(job.result) if isinstance(job.result, Mapping) else None,
        )

    @staticmethod
    def _release(
        session: Session, owner_kind: str, owner_id: str, now: datetime
    ) -> None:
        for reservation in session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == owner_kind,
                ResourceReservation.owner_id == owner_id,
                ResourceReservation.state == "active",
            )
        ):
            reservation.state = "released"
            reservation.released_at = now


def _required_string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise RecipeOperationConflict(f"operation {key} is invalid")
    return item


def _topology_order(document: Mapping[str, object], key: str) -> tuple[str, ...]:
    try:
        topology = recipe_topology(document)
    except Exception as error:
        raise RecipeOperationConflict("recipe topology is invalid") from error
    order = topology.get(key)
    if not isinstance(order, list) or not all(isinstance(role, str) for role in order):
        raise RecipeOperationConflict(f"recipe topology {key} is invalid")
    return tuple(order)


def _role_phases(
    order: Sequence[str],
    node_payloads: Sequence[tuple[str, Mapping[str, object]]],
    *,
    include_role: bool = True,
) -> tuple[tuple[tuple[str, Mapping[str, object]], ...], ...]:
    by_role: dict[str, list[tuple[str, Mapping[str, object]]]] = {}
    for node_id, payload in node_payloads:
        role = payload.get("role")
        if not isinstance(role, str):
            raise RecipeOperationConflict("operation role is invalid")
        projected = dict(payload)
        if not include_role:
            projected.pop("role")
        by_role.setdefault(role, []).append((node_id, projected))
    if set(by_role) != set(order) or len(set(order)) != len(order):
        raise RecipeOperationConflict("operation topology order is invalid")
    return tuple(
        tuple(sorted(by_role[role], key=lambda item: item[0])) for role in order
    )


def _stored_phases(
    payload: Mapping[str, object],
) -> tuple[tuple[tuple[str, Mapping[str, object]], ...], ...]:
    raw_phases = payload.get("phases")
    if raw_phases is None:
        return ()
    if not isinstance(raw_phases, list) or not raw_phases:
        raise RecipeOperationConflict("stored operation phases are invalid")
    phases: list[tuple[tuple[str, Mapping[str, object]], ...]] = []
    seen: set[str] = set()
    for raw_phase in raw_phases:
        if not isinstance(raw_phase, list) or not raw_phase:
            raise RecipeOperationConflict("stored operation phases are invalid")
        group: list[tuple[str, Mapping[str, object]]] = []
        for raw_item in raw_phase:
            if not isinstance(raw_item, Mapping):
                raise RecipeOperationConflict("stored operation phases are invalid")
            node_id = raw_item.get("node_id")
            item_payload = raw_item.get("payload")
            if not isinstance(node_id, str) or not isinstance(item_payload, Mapping):
                raise RecipeOperationConflict("stored operation phases are invalid")
            if node_id in seen:
                raise RecipeOperationConflict("stored operation phases are invalid")
            seen.add(node_id)
            group.append((node_id, dict(item_payload)))
        phases.append(tuple(group))
    return tuple(phases)


def _current_phase_index(
    children: Sequence[AgentOperation],
    phases: Sequence[Sequence[tuple[str, Mapping[str, object]]]],
) -> int | None:
    child_nodes = {child.node_id for child in children}
    for index in range(len(phases) - 1, -1, -1):
        phase = phases[index]
        phase_nodes = {node_id for node_id, _payload in phase}
        if phase_nodes <= child_nodes:
            return index
    return None


def _valid_image_import_payload(value: object, expected_build_id: str) -> bool:
    expected_fields = {
        "schema_version",
        "kind",
        "build_id",
        "mapping_id",
        "mapping_generation",
        "source_node_id",
        "image_digest",
        "oci_layout_sha256",
        "image_bytes",
    }
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        return False
    image_digest = value.get("image_digest")
    layout_digest = value.get("oci_layout_sha256")
    return (
        value.get("schema_version") == 1
        and value.get("kind") == "recipe.image.import.v1"
        and value.get("build_id") == expected_build_id
        and isinstance(value.get("mapping_id"), str)
        and isinstance(value.get("mapping_generation"), int)
        and value["mapping_generation"] >= 1
        and isinstance(value.get("source_node_id"), str)
        and isinstance(image_digest, str)
        and len(image_digest) == 71
        and image_digest.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in image_digest[7:])
        and isinstance(layout_digest, str)
        and len(layout_digest) == 64
        and all(character in "0123456789abcdef" for character in layout_digest)
        and isinstance(value.get("image_bytes"), int)
        and value["image_bytes"] > 0
    )


def _record_build_evidence(
    session: Session,
    build: RecipeBuild,
    evidence: Mapping[str, object],
    *,
    now: datetime,
) -> None:
    # A retried build may already be present in this transaction's identity map
    # with the previous attempt's upload fields. Refresh under the row lock so
    # terminal evidence is compared with the upload transaction that just
    # completed, not with stale in-memory values.
    session.refresh(build, with_for_update=True)
    expected = {
        "build_input_sha256",
        "image_bytes",
        "image_digest",
        "oci_layout_sha256",
        "policy",
    }
    policy = evidence.get("policy")
    findings = policy.get("findings") if isinstance(policy, Mapping) else None
    image_digest = evidence.get("image_digest")
    layout_digest = evidence.get("oci_layout_sha256")
    image_bytes = evidence.get("image_bytes")
    if (
        set(evidence) != expected
        or evidence.get("build_input_sha256") != build.build_input_sha256
        or not isinstance(image_digest, str)
        or len(image_digest) != 71
        or not image_digest.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in image_digest[7:])
        or not isinstance(layout_digest, str)
        or len(layout_digest) != 64
        or any(character not in "0123456789abcdef" for character in layout_digest)
        or not isinstance(image_bytes, int)
        or isinstance(image_bytes, bool)
        or not 1 <= image_bytes <= 16 * 1024**4
        or not isinstance(policy, Mapping)
        or policy.get("passed") is not True
        or not isinstance(findings, (list, tuple))
        or bool(findings)
        or policy.get("dockerfile") != build.plan.get("dockerfile")
        or build.image_digest not in {None, image_digest}
        or build.oci_layout_sha256 not in {None, layout_digest}
        or build.image_bytes not in {None, image_bytes}
    ):
        raise RecipeOperationConflict("recipe build evidence is invalid")
    build.state = "succeeded"
    build.image_digest = image_digest
    build.oci_layout_sha256 = layout_digest
    build.image_bytes = image_bytes
    build.error = None
    build.updated_at = now


def _record_image_import_evidence(
    session: Session,
    operation: AgentOperation,
    evidence: Mapping[str, object],
    succeeded: bool,
    now: datetime,
) -> None:
    if not succeeded:
        return
    expected = {"build_id", "image_bytes", "image_digest", "oci_layout_sha256"}
    build_id = operation.payload.get("build_id")
    build = session.get(RecipeBuild, build_id) if isinstance(build_id, str) else None
    if (
        set(evidence) != expected
        or build is None
        or build.state != "succeeded"
        or evidence.get("build_id") != build.id
        or evidence.get("image_digest") != build.image_digest
        or evidence.get("oci_layout_sha256") != build.oci_layout_sha256
        or evidence.get("image_bytes") != build.image_bytes
        or operation.payload.get("image_digest") != build.image_digest
        or operation.payload.get("oci_layout_sha256") != build.oci_layout_sha256
        or operation.payload.get("image_bytes") != build.image_bytes
    ):
        raise RecipeOperationConflict("recipe image import evidence is invalid")
    assert build.image_digest is not None and build.image_bytes is not None
    raw_digest = build.image_digest[7:]
    artifact = session.scalar(
        select(NodeArtifact).where(
            NodeArtifact.node_id == operation.node_id,
            NodeArtifact.digest == raw_digest,
        )
    )
    if artifact is None:
        session.add(
            NodeArtifact(
                node_id=operation.node_id,
                kind="image",
                digest=raw_digest,
                source=f"docker-archive:{build.oci_layout_sha256}",
                size_bytes=build.image_bytes,
                state="verified",
                ref_count=0,
                verified_at=now,
                updated_at=now,
            )
        )
    elif (
        artifact.kind != "image"
        or artifact.size_bytes != build.image_bytes
        or artifact.state != "verified"
    ):
        raise RecipeOperationConflict(
            "recipe image import conflicts with local artifact state"
        )


def _validate_start_evidence(
    session: Session,
    run_id: str,
    operation: AgentOperation,
    evidence: Mapping[str, object],
) -> tuple[str, str]:
    expected_fields = {
        "recipe_revision_id",
        "recipe_content_sha256",
        "image_digest",
        "artifact_set_digest",
        "model_identity",
        "rank",
        "world_size",
        "endpoint",
        "memory_reservation_bytes",
        "ready",
        "evidence_digest",
    }
    if set(evidence) != expected_fields or evidence.get("ready") is not True:
        raise RecipeOperationConflict("start evidence is invalid")
    run = session.get(RecipeRun, run_id)
    installation = (
        session.get(RecipeInstallation, run.installation_id)
        if run is not None
        else None
    )
    revision = (
        session.get(LocalRecipeRevision, installation.recipe_revision_id)
        if installation is not None
        else None
    )
    if revision is None:
        raise RecipeOperationConflict("start evidence authority is unavailable")
    artifacts = revision.document.get("artifacts")
    first = artifacts[0] if isinstance(artifacts, list) and artifacts else None
    if not isinstance(first, Mapping):
        raise RecipeOperationConflict("start evidence authority is invalid")
    image_digest = installation.image_digest.removeprefix("sha256:")
    model_identity = f"{first.get('repository')}@{first.get('revision')}"
    endpoint_address = operation.payload.get("endpoint_address")
    port = operation.payload.get("port")
    try:
        parsed_address = ipaddress.ip_address(endpoint_address)
    except (TypeError, ValueError) as error:
        raise RecipeOperationConflict("start evidence authority is invalid") from error
    host = f"[{parsed_address}]" if parsed_address.version == 6 else str(parsed_address)
    expected_endpoint = f"http://{host}:{port}"
    comparisons = {
        "recipe_revision_id": operation.payload.get("recipe_revision_id"),
        "recipe_content_sha256": operation.payload.get("recipe_content_sha256"),
        "image_digest": image_digest,
        "model_identity": model_identity,
        "rank": operation.payload.get("rank"),
        "world_size": operation.payload.get("world_size"),
        "endpoint": expected_endpoint,
        "memory_reservation_bytes": operation.payload.get("reserved_memory_bytes"),
    }
    if any(evidence.get(key) != value for key, value in comparisons.items()):
        raise RecipeOperationConflict(
            "start evidence does not match its fenced request"
        )
    artifact_set_digest = evidence.get("artifact_set_digest")
    if (
        not isinstance(artifact_set_digest, str)
        or len(artifact_set_digest) != 64
        or any(character not in "0123456789abcdef" for character in artifact_set_digest)
    ):
        raise RecipeOperationConflict("start artifact evidence is invalid")
    digest = evidence.get("evidence_digest")
    identity = {
        key: value for key, value in evidence.items() if key != "evidence_digest"
    }
    observed_digest = hashlib.sha256(canonical_message(identity)).hexdigest()
    if not isinstance(digest, str) or digest != observed_digest:
        raise RecipeOperationConflict("start evidence digest is invalid")
    return expected_endpoint, digest


def _aware(value: datetime) -> datetime:
    return (
        value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    ).astimezone(UTC)


def record_recipe_run_observations(
    sessions: sessionmaker[Session],
    node_id: str,
    observed_at: datetime,
    observations: tuple[RecipeRunObservation, ...],
) -> None:
    """Project one authenticated node's complete local recipe-run snapshot."""

    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("recipe run observation time must be timezone-aware")
    observed = observed_at.astimezone(UTC)
    by_run: dict[str, bool] = {}
    for observation in observations:
        if not isinstance(observation.ready, bool):
            raise TypeError("recipe run readiness must be boolean")
        if observation.run_id in by_run:
            raise ValueError("recipe run observation is duplicated")
        by_run[observation.run_id] = observation.ready
    with sessions.begin() as session:
        assigned = tuple(
            session.scalars(
                select(RunNode)
                .join(RecipeRun, RecipeRun.id == RunNode.run_id)
                .where(
                    RunNode.node_id == node_id,
                    RecipeRun.state == "running",
                )
                .order_by(RunNode.run_id)
            )
        )
        for node in assigned:
            if _aware(node.updated_at) > observed:
                continue
            node.state = "running" if by_run.get(node.run_id, False) else "failed"
            node.updated_at = observed


__all__ = [
    "RecipeOperationConflict",
    "RecipeOperationService",
    "RecipeOperationView",
    "RecipeRunObservation",
    "RecipeRunRankStatus",
    "RecipeRunStatus",
    "record_recipe_run_observations",
]
