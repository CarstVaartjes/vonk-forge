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

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import canonical_message

from .cluster_mappings import ClusterMappingPlan, ClusterMappingService
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
from .recipe_builds import RecipeBuildPlan, RecipeBuildService
from .run_admission import RunAdmissionService, RunPlan
from .source_policy import SourcePolicyReport


class AgentJobQueue(Protocol):
    def enqueue_in_session(
        self,
        session: Session,
        parent_job_id: str,
        node_id: str,
        operation: str,
        base_commit: str,
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


_TERMINAL_JOB_STATES = frozenset({"succeeded", "failed", "expired"})


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
        self._builds = builds
        self._mappings = mappings
        self._run_health_maximum_age = timedelta(seconds=run_health_maximum_age_seconds)

    def preview_mapping(
        self,
        recipe_revision_id: str,
        profile_name: str,
        node_ids: tuple[str, ...],
        *,
        parameters: Mapping[str, object],
    ) -> ClusterMappingPlan:
        if self._mappings is None:
            raise RecipeOperationConflict("cluster mapping service is unavailable")
        return self._mappings.plan(
            recipe_revision_id, profile_name, node_ids, parameters=parameters
        )

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
                    base_commit=succeeded.base_commit,
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

    def distribute_image(
        self,
        recipe_build_id: str,
        mapping_id: str,
        *,
        mapping_generation: int,
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
        plan_digest = hashlib.sha256(canonical_message(identity)).hexdigest()
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

    def preview_run(self, installation_id: str) -> RunPlan:
        return self._run_admission.plan_run(installation_id, now=self._clock())

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
        alias: str,
        actor: str,
        request_id: str,
    ) -> RecipeOperationView:
        existing = self._idempotent(request_id, "recipe.start", plan_digest)
        if existing is not None:
            return existing
        if plan_digest != plan.plan_digest:
            raise RecipeOperationConflict(
                "submitted plan digest does not match preview"
            )
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
            master = next((node for node in plan.nodes if node.rank == 0), None)
            if master is None:
                raise RecipeOperationConflict("recipe run has no rank-zero entrypoint")
            world_size = len(plan.nodes)
            master_address = master.fabric_address if world_size > 1 else None
            master_port = master.rendezvous_port if world_size > 1 else None
            if world_size > 1 and (master_address is None or master_port is None):
                raise RecipeOperationConflict(
                    "recipe direct-fabric rendezvous is unavailable"
                )
            try:
                run_id = self._run_admission.accept_run_in_session(
                    session, plan, alias=alias, actor=actor, now=now
                )
            except (RuntimeError, ValueError) as error:
                raise RecipeOperationConflict(str(error)) from error
            run = session.get(RecipeRun, run_id)
            revision = session.get(LocalRecipeRevision, plan.recipe_revision_id)
            installation = session.get(RecipeInstallation, plan.installation_id)
            assert run is not None and revision is not None and installation is not None
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
                            "alias": alias,
                            "rank": node.rank,
                            "role": node.role,
                            "port": node.port,
                            "reserved_memory_bytes": node.required_memory_bytes,
                            "endpoint_address": presences[node.node_id],
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
                authority_digest=recipe_digest,
                now=now,
            )
        self._agent_jobs.notify_available()
        return self.get(job.id)

    def stop(self, run_id: str, *, actor: str, request_id: str) -> RecipeOperationView:
        existing = self._idempotent(request_id, "recipe.stop", None)
        if existing is not None:
            return existing
        with self._sessions() as session:
            run = session.get(RecipeRun, run_id)
            if run is None:
                raise RecipeOperationConflict("recipe run does not exist")
            if run.state not in {"starting", "running", "failed", "lost"}:
                raise RecipeOperationConflict("recipe run is not stoppable")
            plan_digest = run.plan_digest
        # Route removal is deliberately synchronous and precedes creation of
        # stop work. If withdrawal fails, no stop command can race a live route.
        self._route_withdrawer(run_id)
        now = self._clock()
        with self._sessions.begin() as session:
            run = session.get(RecipeRun, run_id, with_for_update=True)
            if run is None:
                raise RecipeOperationConflict("recipe run does not exist")
            if run.state not in {"starting", "running", "failed", "lost"}:
                raise RecipeOperationConflict("recipe run is not stoppable")
            if run.plan_digest != plan_digest:
                raise RecipeOperationConflict(
                    "recipe run changed before stop admission"
                )
            nodes = tuple(
                session.scalars(
                    select(RunNode)
                    .where(RunNode.run_id == run_id)
                    .order_by(RunNode.rank)
                )
            )
            run.state = "stopping"
            run.route_state = "withdrawn"
            run.route_error = None
            run.updated_at = now
            job = self._queue_in_session(
                session,
                kind="recipe.stop",
                owner_kind="run",
                owner_id=run_id,
                plan_digest=plan_digest,
                actor=actor,
                request_id=request_id,
                node_payloads=tuple(
                    (
                        node.node_id,
                        {
                            "schema_version": 1,
                            "run_id": run_id,
                            "plan_digest": plan_digest,
                        },
                    )
                    for node in nodes
                ),
                authority_digest=plan_digest,
                now=now,
            )
        self._agent_jobs.notify_available()
        return self.get(job.id)

    def uninstall(
        self, installation_id: str, *, actor: str, request_id: str
    ) -> RecipeOperationView:
        existing = self._idempotent(request_id, "recipe.uninstall", None)
        if existing is not None:
            return existing
        with self._sessions() as session:
            installation = session.get(RecipeInstallation, installation_id)
            if installation is None:
                raise RecipeOperationConflict("recipe installation does not exist")
            active_run = session.scalar(
                select(RecipeRun.id).where(
                    RecipeRun.installation_id == installation_id,
                    RecipeRun.state.not_in({"stopped"}),
                )
            )
            if active_run is not None:
                raise RecipeOperationConflict("installation has an active run")
            if installation.state not in {"installed", "partial", "failed"}:
                raise RecipeOperationConflict(
                    "recipe installation is not uninstallable"
                )
            nodes = tuple(
                session.scalars(
                    select(InstallationNode)
                    .where(InstallationNode.installation_id == installation_id)
                    .order_by(InstallationNode.node_id)
                )
            )
            plan_digest = installation.plan_digest
            revision = session.get(LocalRecipeRevision, installation.recipe_revision_id)
            assert revision is not None and revision.content_sha256 is not None
            recipe_digest = revision.content_sha256
        return self._queue(
            kind="recipe.uninstall",
            owner_kind="installation",
            owner_id=installation_id,
            plan_digest=plan_digest,
            actor=actor,
            request_id=request_id,
            node_payloads=tuple(
                (
                    node.node_id,
                    {
                        "schema_version": 1,
                        "installation_id": installation_id,
                        "recipe_content_sha256": recipe_digest,
                        "plan_digest": plan_digest,
                    },
                )
                for node in nodes
            ),
            authority_digest=recipe_digest,
        )

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
            elif previous.kind == "recipe.install" and previous.state == "failed":
                job = self._retry_install_in_session(
                    session, previous, actor=actor, request_id=request_id, now=now
                )
            else:
                raise RecipeOperationConflict("recipe operation is not retryable")
        self._agent_jobs.notify_available()
        return self.get(job.id)

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
        terminal = all(child.state in _TERMINAL_JOB_STATES for child in children)
        if terminal:
            successful = sorted(
                child.node_id for child in children if child.state == "succeeded"
            )
            failed = sorted(
                child.node_id for child in children if child.state == "failed"
            )
            job.state = "failed" if failed else "succeeded"
            job.result = {
                "successful_nodes": successful,
                "failed_nodes": failed,
                "node_evidence": node_evidence,
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
                if failed:
                    run.state = "stopping"
                    run.route_state = "withdrawn"
                    run.route_error = (
                        "one or more ranks failed to start; cleanup queued"
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
                        run_nodes = tuple(
                            session.scalars(
                                select(RunNode)
                                .where(RunNode.run_id == owner_id)
                                .order_by(RunNode.rank)
                            )
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
                                for run_node in run_nodes
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
                run.state = "failed" if failed else "stopped"
                run.stopped_at = now if not failed else None
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

    def _idempotent(
        self, request_id: str, kind: str, plan_digest: str | None
    ) -> RecipeOperationView | None:
        with self._sessions() as session:
            existing = session.scalar(select(Job).where(Job.request_id == request_id))
            if existing is None:
                return None
            existing_digest = existing.payload.get("plan_digest")
            if existing.kind != kind or (
                plan_digest is not None and existing_digest != plan_digest
            ):
                raise RecipeOperationConflict(
                    "request key was already used differently"
                )
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
    ) -> Job:
        if not node_payloads:
            raise RecipeOperationConflict("operation group has no target nodes")
        if session.scalar(select(Job.id).where(Job.request_id == request_id)):
            raise RecipeOperationConflict("request key was already used differently")
        job_id = str(uuid.uuid4())
        targets = sorted(node_id for node_id, _payload in node_payloads)
        job_payload: dict[str, object] = {
            "schema_version": 1,
            "owner_kind": owner_kind,
            "owner_id": owner_id,
            "plan_digest": plan_digest,
        }
        job = Job(
            id=job_id,
            request_id=request_id,
            kind=kind,
            state="running",
            actor=actor,
            base_commit=authority_digest[:40],
            targets=targets,
            payload_digest=hashlib.sha256(canonical_message(job_payload)).hexdigest(),
            payload=job_payload,
            created_at=now,
            updated_at=now,
        )
        session.add(job)
        session.flush()
        for node_id, payload in node_payloads:
            self._agent_jobs.enqueue_in_session(
                session,
                job_id,
                node_id,
                kind,
                authority_digest[:40],
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
    raw_digest = image_digest[7:]
    existing = session.scalar(
        select(NodeArtifact).where(
            NodeArtifact.node_id == build.builder_node_id,
            NodeArtifact.digest == raw_digest,
        )
    )
    if existing is None:
        session.add(
            NodeArtifact(
                node_id=build.builder_node_id,
                kind="image",
                digest=raw_digest,
                source=f"oci-layout:{layout_digest}",
                size_bytes=image_bytes,
                state="verified",
                ref_count=0,
                verified_at=now,
                updated_at=now,
            )
        )


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
                source=f"oci-layout:{build.oci_layout_sha256}",
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
