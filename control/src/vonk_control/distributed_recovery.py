"""Durable controller coordination for distributed recipe recovery."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import canonical_message

from .distributed_lifecycle import DistributedLifecycleError
from .models import (
    AgentOperation,
    AgentPresence,
    Job,
    LocalRecipeRevision,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from .recipe_contract import recipe_topology


class _RecoveryJobQueue(Protocol):
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


class _RecoveryRoutes(Protocol):
    def publication_transaction(self): ...

    def withdraw_run_in_session(self, session: Session, run_id: str): ...


class DistributedRecoveryCoordinator:
    """Queue one durable, bounded stop/start recovery for a failed exact rank set."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        routes: _RecoveryRoutes,
        agent_jobs: _RecoveryJobQueue,
        clock: Callable[[], datetime],
    ) -> None:
        self._sessions = sessions
        self._routes = routes
        self._agent_jobs = agent_jobs
        self._clock = clock

    def tick(self) -> bool:
        now = _aware(self._clock())
        queued = False
        worked = False
        with self._routes.publication_transaction() as session:
            candidates = tuple(
                session.scalars(
                    select(RecipeRun)
                    .where(RecipeRun.state == "running")
                    .order_by(RecipeRun.created_at, RecipeRun.id)
                    .with_for_update(of=RecipeRun)
                )
            )
            for run in candidates:
                failed = tuple(
                    session.scalars(
                        select(RunNode)
                        .where(RunNode.run_id == run.id, RunNode.state == "failed")
                        .order_by(RunNode.rank)
                    )
                )
                if not failed:
                    continue
                if run.route_state == "published":
                    self._routes.withdraw_run_in_session(session, run.id)
                    run.route_state = "withdrawn"
                    run.updated_at = now
                    worked = True
                if self._active_recovery(session, run.id):
                    if worked:
                        break
                    continue
                try:
                    authority = _recovery_authority(
                        session, run, now, failed[0].rank
                    )
                except DistributedLifecycleError as error:
                    run.state = "failed"
                    run.route_state = "withdrawn"
                    run.route_error = str(error)[:512]
                    run.updated_at = now
                    worked = True
                    break
                if authority is None:
                    run.state = "failed"
                    run.route_state = "withdrawn"
                    run.route_error = "failed rank has no distributed recovery policy"
                    run.updated_at = now
                    worked = True
                    break
                job = _enqueue_recovery_stop(
                    session,
                    self._agent_jobs,
                    run,
                    authority,
                    failed_rank=failed[0].rank,
                    now=now,
                )
                run.route_state = "withdrawn"
                run.route_error = f"distributed recovery queued: {job.id}"
                run.updated_at = now
                queued = True
                worked = True
                break
        if queued:
            self._agent_jobs.notify_available()
        return worked

    @staticmethod
    def _active_recovery(session: Session, run_id: str) -> bool:
        jobs = session.scalars(
            select(Job).where(
                Job.kind.in_({"recipe.start", "recipe.stop"}),
                Job.state.in_({"queued", "running"}),
            )
        )
        return any(
            job.payload.get("owner_id") == run_id
            and isinstance(job.payload.get("recovery"), Mapping)
            for job in jobs
        )


def recovery_start_plan(
    payload: Mapping[str, object], *, now: datetime
) -> (
    tuple[tuple[tuple[tuple[str, Mapping[str, object]], ...], ...], dict[str, object]]
    | None
):
    """Decode the trusted start phases carried by a recovery stop job."""

    value = payload.get("recovery")
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "failed_rank",
        "deadline",
        "start_phases",
    }:
        raise DistributedLifecycleError("distributed recovery authority is invalid")
    failed_rank = value.get("failed_rank")
    deadline_value = value.get("deadline")
    if (
        value.get("schema_version") != 1
        or type(failed_rank) is not int
        or failed_rank < 0
        or not isinstance(deadline_value, str)
    ):
        raise DistributedLifecycleError("distributed recovery authority is invalid")
    try:
        deadline = datetime.fromisoformat(deadline_value)
    except ValueError as error:
        raise DistributedLifecycleError(
            "distributed recovery authority is invalid"
        ) from error
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        raise DistributedLifecycleError("distributed recovery authority is invalid")
    if _aware(now) >= _aware(deadline):
        raise DistributedLifecycleError("distributed recovery deadline elapsed")
    phases = _decode_phases(value.get("start_phases"))
    marker = {
        "schema_version": 1,
        "failed_rank": failed_rank,
        "deadline": deadline_value,
    }
    return phases, marker


def _recovery_authority(
    session: Session,
    run: RecipeRun,
    now: datetime,
    failed_rank: int,
) -> dict[str, object] | None:
    installation = session.get(RecipeInstallation, run.installation_id)
    revision = (
        session.get(LocalRecipeRevision, installation.recipe_revision_id)
        if installation is not None
        else None
    )
    if (
        installation is None
        or revision is None
        or revision.content_sha256 is None
        or installation.image_digest is None
    ):
        raise DistributedLifecycleError("distributed recovery authority is missing")
    topology = recipe_topology(revision.document)
    runtime = revision.document.get("runtime")
    lifecycle = runtime.get("lifecycle") if isinstance(runtime, Mapping) else None
    if topology.get("mode") != "distributed" or not isinstance(lifecycle, Mapping):
        return None
    failure = lifecycle.get("failure")
    readiness = lifecycle.get("readiness")
    if (
        not isinstance(failure, Mapping)
        or failure.get("rank_loss") != "withdraw-endpoint"
        or failure.get("recovery") != "restart-worker-then-entrypoint"
        or not isinstance(readiness, Mapping)
        or readiness.get("strategy") != "endpoint-owner-after-all-ranks"
    ):
        return None
    readiness_timeout = readiness.get("timeout_seconds")
    stop_timeout = lifecycle.get("stop_timeout_seconds")
    if (
        type(readiness_timeout) is not int
        or not 1 <= readiness_timeout <= 3600
        or type(stop_timeout) is not int
        or not 1 <= stop_timeout <= 600
    ):
        raise DistributedLifecycleError("distributed recovery timeout is invalid")
    nodes = tuple(
        session.scalars(
            select(RunNode).where(RunNode.run_id == run.id).order_by(RunNode.rank)
        )
    )
    if (
        tuple(node.rank for node in nodes) != tuple(range(len(nodes)))
        or len(nodes) != topology.get("node_count")
        or failed_rank not in {node.rank for node in nodes}
    ):
        raise DistributedLifecycleError("distributed recovery rank set is invalid")
    plans = run.plan.get("nodes") if isinstance(run.plan, Mapping) else None
    if not isinstance(plans, list) or len(plans) != len(nodes):
        raise DistributedLifecycleError("distributed recovery plan is invalid")
    by_rank = {item.get("rank"): item for item in plans if isinstance(item, Mapping)}
    owner = next(
        (
            item
            for item in plans
            if isinstance(item, Mapping) and item.get("endpoint_owner") is True
        ),
        None,
    )
    if len(by_rank) != len(nodes) or not isinstance(owner, Mapping):
        raise DistributedLifecycleError("distributed recovery plan is invalid")
    master_address = owner.get("fabric_address")
    master_port = owner.get("rendezvous_port")
    if not isinstance(master_address, str) or type(master_port) is not int:
        raise DistributedLifecycleError("distributed recovery rendezvous is invalid")
    presences: dict[str, str] = {}
    for node in nodes:
        presence = session.scalar(
            select(AgentPresence)
            .where(AgentPresence.node_id == node.node_id)
            .order_by(AgentPresence.observed_at.desc())
            .limit(1)
        )
        if presence is None:
            raise DistributedLifecycleError(
                "distributed recovery endpoint evidence is missing"
            )
        presences[node.node_id] = presence.management_address
    start_payloads: dict[str, tuple[str, dict[str, object]]] = {}
    for node in nodes:
        plan = by_rank[node.rank]
        local_address = plan.get("fabric_address")
        endpoint_owner = plan.get("endpoint_owner")
        if not isinstance(local_address, str) or type(endpoint_owner) is not bool:
            raise DistributedLifecycleError("distributed recovery plan is invalid")
        start_payloads[node.role] = (
            node.node_id,
            {
                "schema_version": 1,
                "run_id": run.id,
                "installation_id": installation.id,
                "recipe_revision_id": revision.id,
                "recipe_content_sha256": revision.content_sha256,
                "mapping_id": run.mapping_id,
                "mapping_generation": run.mapping_generation,
                "image_digest": installation.image_digest,
                "plan_digest": run.plan_digest,
                "alias": run.alias,
                "rank": node.rank,
                "role": node.role,
                "port": node.port,
                "reserved_memory_bytes": node.reserved_memory_bytes,
                "endpoint_address": (
                    presences[node.node_id] if endpoint_owner else local_address
                ),
                "world_size": len(nodes),
                "local_address": local_address,
                "master_address": master_address,
                "master_port": master_port,
            },
        )
    start_order = topology.get("start_order")
    stop_order = topology.get("stop_order")
    roles = {node.role for node in nodes}
    if (
        not isinstance(start_order, list)
        or not isinstance(stop_order, list)
        or set(start_order) != roles
        or set(stop_order) != roles
        or len(start_order) != len(roles)
        or len(stop_order) != len(roles)
    ):
        raise DistributedLifecycleError("distributed recovery order is invalid")
    return {
        "deadline": (
            now + timedelta(seconds=min(readiness_timeout, stop_timeout))
        ).isoformat(),
        "failed_rank": failed_rank,
        "recipe_content_sha256": revision.content_sha256,
        "start_phases": [[start_payloads[str(role)]] for role in start_order],
        "stop_phases": [
            [
                (
                    node.node_id,
                    {
                        "schema_version": 1,
                        "run_id": run.id,
                        "plan_digest": run.plan_digest,
                    },
                )
                for node in nodes
                if node.role == role
            ]
            for role in stop_order
        ],
    }


def _enqueue_recovery_stop(
    session: Session,
    queue: _RecoveryJobQueue,
    run: RecipeRun,
    authority: Mapping[str, object],
    *,
    failed_rank: int,
    now: datetime,
) -> Job:
    raw_stop_phases = authority.get("stop_phases")
    raw_start_phases = authority.get("start_phases")
    recipe_digest = authority.get("recipe_content_sha256")
    deadline = authority.get("deadline")
    if (
        not isinstance(raw_stop_phases, list)
        or not isinstance(raw_start_phases, list)
        or not isinstance(recipe_digest, str)
        or not isinstance(deadline, str)
    ):
        raise DistributedLifecycleError("distributed recovery authority is invalid")
    stop_phases = tuple(tuple(group) for group in raw_stop_phases)
    start_phases = tuple(tuple(group) for group in raw_start_phases)
    request_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"vonk:distributed-recovery:{run.id}:{failed_rank}:{deadline}",
        )
    )
    if session.scalar(select(Job.id).where(Job.request_id == request_id)):
        raise DistributedLifecycleError("distributed recovery is already queued")
    job_id = str(uuid.uuid4())
    job_payload = {
        "schema_version": 1,
        "owner_kind": "run",
        "owner_id": run.id,
        "plan_digest": run.plan_digest,
        "phases": _encode_phases(stop_phases),
        "recovery": {
            "schema_version": 1,
            "failed_rank": failed_rank,
            "deadline": deadline,
            "start_phases": _encode_phases(start_phases),
        },
    }
    targets = sorted(node_id for group in stop_phases for node_id, _payload in group)
    job = Job(
        id=job_id,
        request_id=request_id,
        kind="recipe.stop",
        state="running",
        actor="system:distributed-recovery",
        base_commit=recipe_digest[:40],
        targets=targets,
        payload_digest=hashlib.sha256(canonical_message(job_payload)).hexdigest(),
        payload=job_payload,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.flush()
    for node_id, payload in stop_phases[0]:
        queue.enqueue_in_session(
            session,
            job.id,
            node_id,
            "recipe.stop",
            recipe_digest[:40],
            payload,
            operation_id=str(uuid.uuid4()),
        )
    return job


def _encode_phases(
    phases: Sequence[Sequence[tuple[str, Mapping[str, object]]]],
) -> list[list[dict[str, object]]]:
    return [
        [
            {"node_id": node_id, "payload": json.loads(canonical_message(payload))}
            for node_id, payload in group
        ]
        for group in phases
    ]


def _decode_phases(
    value: object,
) -> tuple[tuple[tuple[str, Mapping[str, object]], ...], ...]:
    if not isinstance(value, list) or not value:
        raise DistributedLifecycleError("distributed recovery phases are invalid")
    phases: list[tuple[tuple[str, Mapping[str, object]], ...]] = []
    seen: set[str] = set()
    for raw_group in value:
        if not isinstance(raw_group, list) or not raw_group:
            raise DistributedLifecycleError("distributed recovery phases are invalid")
        group: list[tuple[str, Mapping[str, object]]] = []
        for item in raw_group:
            if not isinstance(item, Mapping) or set(item) != {"node_id", "payload"}:
                raise DistributedLifecycleError(
                    "distributed recovery phases are invalid"
                )
            node_id = item.get("node_id")
            item_payload = item.get("payload")
            if (
                not isinstance(node_id, str)
                or node_id in seen
                or not isinstance(item_payload, Mapping)
            ):
                raise DistributedLifecycleError(
                    "distributed recovery phases are invalid"
                )
            seen.add(node_id)
            group.append((node_id, dict(item_payload)))
        phases.append(tuple(group))
    return tuple(phases)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DistributedLifecycleError("distributed recovery clock is invalid")
    return value.astimezone(UTC)


__all__ = ["DistributedRecoveryCoordinator", "recovery_start_plan"]
