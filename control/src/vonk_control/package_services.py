"""Production projections for the generic workload package API.

The package routes are intentionally thin.  This module is the production
adapter that binds them to the existing Git repository and operational
database; it does not add a second queue, reconciler, or trust root.  Read
projections are available in the API process.  Removal, garbage collection,
and rollout are dispatched through the existing worker-owned agent-job
boundary; release publication and validation remain trust-gated until their
signer/runner is installed.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import AgentOperation, PackageReleaseLock, canonical_message

from cluster_profiles.workload_packages import PackageFamily, WorkloadDeployment

from .models import (
    AgentNode,
    Job,
    Observation,
    PackageActionPlan,
    PackageCandidate,
    PackageObservation,
    PackageResolution,
    PackageRollout,
    PackageRolloutNode,
    PackageValidationRun,
)
from .models import AgentOperation as StoredAgentOperation
from .package_rollouts import (
    PackageDesiredStateResolver,
    PackageRolloutOrchestrator,
    _package_payload_for_identity,
    package_operation_payload,
)
from .package_validation import ValidationController, ValidationError


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _raw_digest(value: object) -> str | None:
    if isinstance(value, str):
        raw = value.removeprefix("sha256:")
        if len(raw) == 64 and all(character in "0123456789abcdef" for character in raw):
            return raw
    return None


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _bounded_mapping(value: object, *, maximum: int = 64) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, object] = {}
    for key, item in list(value.items())[:maximum]:
        if not isinstance(key, str):
            continue
        if isinstance(item, (str, int, float, bool)) or item is None:
            result[key] = item
        elif isinstance(item, Mapping):
            result[key] = _bounded_mapping(item, maximum=16)
    return result


class ProductionPackageProjectionService:
    """Bind W11/W15/W16 API projections to Git and SQL state.

    ``repository`` is the same immutable ``RepositoryService`` used by
    desired-state reconciliation.  ``sessions`` is the existing control DB
    session factory.  Mutations are submitted through the existing
    worker-owned agent-job boundary; no alternate queue or transport is
    created here.
    """

    def __init__(
        self,
        repository: Any,
        sessions: sessionmaker[Session],
        *,
        fleet: Callable[[], Mapping[str, object]] | None = None,
        clock: Callable[[], datetime] | None = None,
        agent_jobs: Any | None = None,
        package_trust: Any | Callable[[str, bytes, str], bool] | None = None,
        publication: Any | None = None,
        validation_runner: Any | None = None,
    ) -> None:
        if not callable(getattr(repository, "head", None)):
            raise TypeError("package repository is required")
        self._repository = repository
        self._sessions = sessions
        self._fleet = fleet or (lambda: {"nodes": []})
        self._clock = clock or (lambda: datetime.now(UTC))
        self._agent_jobs = agent_jobs
        self._package_trust = package_trust
        # Publication is deliberately injected from a separate workload-TUF
        # signer boundary.  The API process never owns private release keys.
        self._publication = publication
        # Validation execution is a separate, worker-owned boundary.  The API
        # can persist an exact plan without receiving arbitrary package code or
        # turning a preview into an activation.  A missing runner is a hard
        # failure at apply time, never an implicit local/no-op validation.
        self._validation_runner = validation_runner
        self._rollouts = (
            PackageRolloutOrchestrator(sessions, agent_jobs, clock=self._clock)
            if agent_jobs is not None
            else None
        )
        self._idempotency_lock = threading.RLock()
        self._idempotent: dict[tuple[object, ...], Mapping[str, object]] = {}

    def install_publication(self, publication: Any) -> None:
        """Attach the isolated workload publication service exactly once."""
        if not callable(getattr(publication, "preview", None)) or not callable(
            getattr(publication, "promote", None)
        ):
            raise TypeError("workload publication service is invalid")
        if self._publication is not None and self._publication is not publication:
            raise RuntimeError("workload publication service is already installed")
        self._publication = publication

    def create_action_plan(
        self,
        action: str,
        subject: str,
        request: Mapping[str, object],
        *,
        actor: str | None = None,
        ttl: timedelta = timedelta(minutes=15),
    ) -> str:
        """Persist and return the exact digest for one preview projection."""
        if action not in {
            "package.validate",
            "package.promote",
            "package.rollout",
            "package.rollback",
            "package.repair",
            "package.remove",
            "package.gc",
        }:
            raise ValueError("package action is invalid")
        if not isinstance(subject, str) or not subject or len(subject) > 128:
            raise ValueError("package action subject is invalid")
        if not isinstance(request, Mapping):
            raise TypeError("package action request is invalid")
        canonical = {"action": action, "subject": subject, "request": dict(request)}
        encoded = json.dumps(
            canonical,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if not 2 <= len(encoded) <= 65_536:
            raise ValueError("package action request is too large")
        digest = hashlib.sha256(encoded).hexdigest()
        now = self._clock()
        expires_at = now + ttl
        if ttl <= timedelta(0) or ttl > timedelta(hours=1):
            raise ValueError("package action plan TTL is invalid")
        with self._sessions.begin() as session:
            existing = session.get(PackageActionPlan, digest)
            if existing is None:
                session.add(
                    PackageActionPlan(
                        plan_digest=digest,
                        action=action,
                        subject=subject,
                        request=dict(request),
                        state="planned",
                        actor=actor,
                        expires_at=expires_at,
                        created_at=now,
                        updated_at=now,
                    )
                )
            elif (
                existing.action != action
                or existing.subject != subject
                or existing.request != dict(request)
            ):
                raise ValueError("package action digest collision")
        return "sha256:" + digest

    def consume_action_plan(
        self, digest: str, action: str, subject: str | None = None
    ) -> Mapping[str, object]:
        """Atomically fence an exact preview for an apply operation."""
        raw_digest = _raw_digest(digest)
        if raw_digest is None:
            raise ValueError("package action digest is invalid")
        now = self._clock()
        with self._sessions.begin() as session:
            plan = session.scalar(
                select(PackageActionPlan)
                .where(PackageActionPlan.plan_digest == raw_digest)
                .with_for_update()
            )
            if plan is None:
                raise KeyError(digest)
            if plan.action != action or (subject is not None and plan.subject != subject):
                raise ValueError("package action plan action or subject changed")
            expires_at = plan.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at <= now and plan.state not in {"applied", "failed"}:
                plan.state = "expired"
                plan.updated_at = now
                raise KeyError(digest)
            if plan.state in {"planned", "applying"}:
                plan.state = "applying"
                plan.updated_at = now
            elif plan.state == "applied":
                return dict(plan.request)
            else:
                raise ValueError("package action plan is no longer applicable")
            return dict(plan.request)

    def _action_plan(self, digest: str, action: str) -> PackageActionPlan:
        raw_digest = _raw_digest(digest)
        if raw_digest is None:
            raise ValueError("package action digest is invalid")
        with self._sessions() as session:
            plan = session.get(PackageActionPlan, raw_digest)
            if plan is None or plan.action != action:
                raise KeyError(digest)
            return plan

    def _queue_package_operations(
        self,
        *,
        action: str,
        plan_digest: str,
        request: Mapping[str, object],
        actor: str,
        request_id: str,
    ) -> Mapping[str, object]:
        """Create one fenced parent job and typed outbound operations."""
        if self._agent_jobs is None:
            raise RuntimeError("package worker operation service is not installed")
        node_ids = request.get("node_ids")
        if not isinstance(node_ids, list) or not node_ids or any(
            not isinstance(node_id, str) for node_id in node_ids
        ):
            raise ValueError("package action node set is invalid")
        base_commit = self._repository.head()
        now = self._clock()
        job_id = str(uuid.uuid4())
        targets = tuple(sorted(set(node_ids)))
        payload = {
            "schema_version": 1,
            "action": action,
            "plan_digest": _raw_digest(plan_digest),
            "request": dict(request),
        }
        deployment = None
        if action in {"package.remove", "package.repair", "package.rollback"}:
            deployment_id = request.get("deployment_id")
            release_digest = request.get("release_digest")
            if not isinstance(deployment_id, str) or not isinstance(release_digest, str):
                raise ValueError("package removal identity is invalid")
            deployment = self._deployment_for_release(deployment_id, release_digest)
        with self._sessions.begin() as session:
            session.add(
                Job(
                    id=job_id,
                    request_id=request_id,
                    kind=action,
                    state="running",
                    actor=actor,
                    base_commit=base_commit,
                    targets=list(targets),
                    payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
                    payload=payload,
                    created_at=now,
                    updated_at=now,
                )
            )
            for node_id in targets:
                if action in {"package.remove", "package.repair", "package.rollback"}:
                    assert deployment is not None
                    operation = {
                        "package.remove": AgentOperation.PACKAGE_REMOVE.value,
                        "package.repair": AgentOperation.PACKAGE_REPAIR.value,
                        "package.rollback": AgentOperation.PACKAGE_ROLLBACK.value,
                    }[action]
                    if action == "package.rollback":
                        previous_deployment_digest = request.get("deployment_digest")
                        operation_payload = _package_payload_for_identity(
                            deployment,
                            operation,
                            deployment.release_digest,
                            previous_deployment_digest
                            if isinstance(previous_deployment_digest, str)
                            else None,
                        )
                    else:
                        operation_payload = package_operation_payload(deployment, operation)
                elif action == "package.gc":
                    targets_by_node = request.get("target_bytes_by_node")
                    target_bytes = (
                        targets_by_node.get(node_id, 0)
                        if isinstance(targets_by_node, Mapping)
                        else request.get("target_bytes", 0)
                    )
                    if not isinstance(target_bytes, int) or target_bytes < 0:
                        raise ValueError("package GC target is invalid")
                    if target_bytes < 1:
                        continue
                    operation_payload = {
                        "schema_version": 1,
                        "dry_run": False,
                        "target_bytes": target_bytes,
                    }
                    operation = AgentOperation.PACKAGE_GC.value
                else:
                    raise ValueError("package action dispatch is unsupported")
                self._agent_jobs.enqueue_in_session(
                    session,
                    job_id,
                    node_id,
                    operation,
                    base_commit,
                    operation_payload,
                    operation_id=str(uuid.uuid4()),
                )
        self._agent_jobs.notify_available()
        return {
            "id": job_id,
            "state": "planned",
            "plan_digest": plan_digest,
            "progress": {
                "completed": 0,
                "failed": 0,
                "running": 0,
                "total": len(targets),
            },
            "nodes": [
                {
                    "node_id": node_id,
                    "state": "queued",
                    "batch_index": 0,
                    "completed": 0,
                    "total": 1,
                }
                for node_id in targets
            ],
            "failure": None,
            "job_id": job_id,
            "audit_request_id": request_id,
            "rollback_rollout_id": None,
            "rollback_selector": None,
        }

    def _deployment_for_release(
        self, deployment_id: str, release_digest: str
    ) -> WorkloadDeployment:
        snapshot = self._snapshot()
        path = f"config/workload-deployments/{deployment_id}.toml"
        document = self._repository.read_document(snapshot.commit, path)
        if not isinstance(document.parsed, Mapping):
            raise TypeError("workload deployment document is invalid")
        deployment = WorkloadDeployment.load(document.parsed)
        target = _raw_digest(release_digest)
        if target is None:
            raise ValueError("package release digest is invalid")
        if deployment.release_digest == target:
            return deployment
        updated = dict(json.loads(deployment.canonical_bytes))
        updated["release_digest"] = target
        return WorkloadDeployment.load(updated)

    def finish_action_plan(
        self,
        digest: str,
        *,
        result: Mapping[str, object],
        failed: bool = False,
    ) -> None:
        raw_digest = _raw_digest(digest)
        if raw_digest is None:
            raise ValueError("package action digest is invalid")
        now = self._clock()
        with self._sessions.begin() as session:
            plan = session.get(PackageActionPlan, raw_digest)
            if plan is None:
                raise KeyError(digest)
            plan.state = "failed" if failed else "applied"
            plan.result = dict(result)
            plan.updated_at = now

    # ---- Git-backed definitions -------------------------------------------------

    def _snapshot(self):
        return self._repository.inspect(self._repository.head())

    def _families(self) -> dict[str, PackageFamily]:
        snapshot = self._snapshot()
        families: dict[str, PackageFamily] = {}
        for path in snapshot.documents:
            if not path.startswith("config/package-families/") or not path.endswith(
                ".toml"
            ):
                continue
            document = self._repository.read_document(snapshot.commit, path)
            if isinstance(document.parsed, Mapping):
                family = PackageFamily.load(document.parsed)
                families[family.family_id] = family
        return families

    def families(self, cursor: str | None, limit: int) -> Mapping[str, object]:
        del cursor
        values = []
        for family in sorted(self._families().values(), key=lambda item: item.family_id):
            channels = family.versions.get("channels", ())
            values.append(
                {
                    "id": family.family_id,
                    "promotion_mode": family.promotion.mode,
                    "channels": [item for item in channels if isinstance(item, str)][
                        :64
                    ],
                }
            )
        values = values[:limit]
        return {"families": values, "next_cursor": None, "total": len(values)}

    # ---- SQL-backed candidate/resolution projections ---------------------------

    @staticmethod
    def _candidate_row(session: Session, candidate_id: str) -> PackageCandidate:
        row = session.get(PackageCandidate, candidate_id)
        if row is None:
            raise KeyError(candidate_id)
        return row

    @staticmethod
    def _candidate_value(row: PackageCandidate) -> dict[str, object]:
        summary = _bounded_mapping(row.summary)
        release = summary.get("release")
        release_value: dict[str, object] | None = None
        if isinstance(release, Mapping):
            release_value = {}
            for key in ("release_digest", "lock_digest"):
                value = release.get(key)
                if isinstance(value, str):
                    release_value[key] = value[:128]
            components = release.get("components")
            if isinstance(components, list):
                release_value["components"] = [
                    {
                        "name": item.get("name"),
                        "digest": item.get("digest"),
                        "kind": item.get("kind"),
                    }
                    for item in components[:128]
                    if isinstance(item, Mapping)
                ]
            dependencies = release.get("dependencies")
            if isinstance(dependencies, list):
                release_value["dependencies"] = [
                    item for item in dependencies[:256] if isinstance(item, str)
                ]
            provenance = release.get("provenance")
            if isinstance(provenance, list):
                release_value["provenance"] = [
                    {"kind": item.get("kind"), "digest": item.get("digest")}
                    for item in provenance[:128]
                    if isinstance(item, Mapping)
                ]
            if not release_value:
                release_value = None
        return {
            "id": row.id,
            "family_id": row.family_id,
            "release_key": str(summary.get("release_key", row.source_reference)),
            "upstream_version": row.upstream_version,
            "state": row.state,
            "reason_code": row.reason_code,
            "metadata": _bounded_mapping(summary.get("metadata")),
            "release": release_value,
        }

    def candidates(
        self, family_id: str | None, cursor: str | None, limit: int
    ) -> Mapping[str, object]:
        del cursor
        with self._sessions() as session:
            statement = select(PackageCandidate).order_by(PackageCandidate.id)
            if family_id is not None:
                statement = statement.where(PackageCandidate.family_id == family_id)
            rows = list(session.scalars(statement).fetchmany(limit))
        values = [self._candidate_value(row) for row in rows]
        return {"candidates": values, "next_cursor": None, "total": len(values)}

    def candidate(self, candidate_id: str) -> Mapping[str, object]:
        with self._sessions() as session:
            return self._candidate_value(self._candidate_row(session, candidate_id))

    def publication_candidate(self, candidate_id: str) -> Mapping[str, object]:
        """Load the bounded publication view from SQL plus Git authority.

        Release locks are never copied into the operational database.  The
        publication service re-reads the exact manifest from the current Git
        commit and binds its bytes to the SQL resolution and validation rows.
        """
        commit = self._repository.head()
        with self._sessions() as session:
            candidate = self._candidate_row(session, candidate_id)
            resolution = self._resolution_row(session, candidate_id)
            if resolution.state != "resolved" or not isinstance(resolution.release_digest, str):
                raise ValueError("candidate resolution is not publishable")
            validation = session.scalar(
                select(PackageValidationRun)
                .where(
                    PackageValidationRun.candidate_id == candidate_id,
                    PackageValidationRun.release_digest == resolution.release_digest,
                    PackageValidationRun.state == "passed",
                )
                .order_by(PackageValidationRun.updated_at.desc(), PackageValidationRun.id.desc())
            )
        path = f"manifests/workload-releases/{candidate.family_id}/{resolution.release_digest}.json"
        document = self._repository.read_document(commit, path)
        if not isinstance(document.parsed, Mapping):
            raise TypeError("workload release lock is invalid")
        try:
            canonical_lock = json.dumps(
                document.parsed,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as error:
            raise TypeError("workload release lock is not canonical") from error
        if document.content not in {canonical_lock, canonical_lock + b"\n"}:
            raise ValueError("workload release lock formatting changed")
        if hashlib.sha256(canonical_lock).hexdigest() != resolution.release_digest:
            raise ValueError("workload release lock digest changed")
        family = self._families().get(candidate.family_id)
        if family is None:
            raise KeyError(candidate.family_id)
        policy = dict(family.policy)
        policy.update(
            {
                "mode": family.promotion.mode,
                "automation_identity": family.promotion.automation_identity,
                "failure_budget": family.promotion.failure_budget,
                "canary": dict(family.promotion.canary)
                if isinstance(family.promotion.canary, Mapping)
                else {},
            }
        )
        summary = _bounded_mapping(candidate.summary)
        evidence = summary.get("evidence")
        if not isinstance(evidence, Mapping) and validation is not None:
            evidence = validation.evidence
        return {
            "id": candidate.id,
            "state": candidate.state,
            "family_id": candidate.family_id,
            "release_digest": resolution.release_digest,
            "lock_bytes": canonical_lock,
            "policy": policy,
            "validation": {
                "state": validation.state,
                "digest": _digest(validation.evidence or validation.progress or {}),
            }
            if validation is not None
            else None,
            "evidence": dict(evidence) if isinstance(evidence, Mapping) else None,
            "evidence_digests": tuple(
                str(item)
                for item in summary.get("evidence_digests", ())
                if isinstance(item, str)
            ),
            "builder_identity": summary.get("builder_identity"),
        }

    def _resolution_row(self, session: Session, candidate_id: str) -> PackageResolution:
        self._candidate_row(session, candidate_id)
        row = session.scalar(
            select(PackageResolution)
            .where(PackageResolution.candidate_id == candidate_id)
            .order_by(PackageResolution.updated_at.desc(), PackageResolution.id.desc())
        )
        if row is None:
            raise KeyError(candidate_id)
        return row

    def resolution(self, candidate_id: str) -> Mapping[str, object]:
        with self._sessions() as session:
            row = self._resolution_row(session, candidate_id)
        if row.release_digest is None:
            # The wire response intentionally binds a release only for a
            # resolved row.  Let the route boundary turn pending/unsupported
            # rows into its bounded 422 response rather than emitting a
            # schema-invalid null digest.
            raise ValueError("candidate resolution has no release")
        return {
            "candidate_id": candidate_id,
            "release_digest": "sha256:" + row.release_digest,
            "state": row.state,
        }

    def compatibility(self, candidate_id: str) -> Mapping[str, object]:
        with self._sessions() as session:
            row = self._resolution_row(session, candidate_id)
        summary = _bounded_mapping(row.summary)
        raw_nodes = summary.get("compatible_node_ids", ())
        nodes = [item for item in raw_nodes if isinstance(item, str)] if isinstance(raw_nodes, list) else []
        digest = summary.get("digest")
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            digest = "sha256:" + _digest(summary)
        release = row.release_digest
        if release is None:
            raise ValueError("candidate resolution has no release")
        return {
            "candidate_id": candidate_id,
            "release_digest": "sha256:" + release,
            "digest": digest,
            "compatible_node_ids": nodes[:512],
        }

    # ---- Durable status projections --------------------------------------------

    @staticmethod
    def _progress(value: object) -> dict[str, int]:
        raw = value if isinstance(value, Mapping) else {}
        result: dict[str, int] = {}
        for key in ("completed", "failed", "running", "total"):
            item = raw.get(key, 0)
            result[key] = item if isinstance(item, int) and item >= 0 else 0
        return result

    def validation_status(self, validation_id: str) -> Mapping[str, object]:
        with self._sessions() as session:
            row = session.get(PackageValidationRun, validation_id)
        if row is None:
            raise KeyError(validation_id)
        with self._sessions() as session:
            plans = list(
                session.scalars(
                    select(PackageActionPlan)
                    .where(
                        PackageActionPlan.action == "package.validate",
                        PackageActionPlan.subject == row.candidate_id,
                    )
                    .order_by(PackageActionPlan.updated_at.desc())
                )
            )
        plan_digest = None
        for plan in plans:
            if isinstance(plan.request, Mapping) and plan.request.get("validation_id") == validation_id:
                plan_digest = plan.plan_digest
                break
        if plan_digest is None:
            plan_digest = _digest(
                {"candidate_id": row.candidate_id, "release_digest": row.release_digest}
            )
        return self._validation_response(row, plan_digest=plan_digest)

    # ---- Repository deployment projections -------------------------------------

    def _deployments(self) -> dict[str, WorkloadDeployment]:
        snapshot = self._snapshot()
        result: dict[str, WorkloadDeployment] = {}
        for path in snapshot.documents:
            if not path.startswith("config/workload-deployments/") or not path.endswith(
                ".toml"
            ):
                continue
            document = self._repository.read_document(snapshot.commit, path)
            if isinstance(document.parsed, Mapping):
                deployment = WorkloadDeployment.load(document.parsed)
                result[deployment.deployment_id] = deployment
        return result

    def deployments(self, cursor: str | None, limit: int) -> Mapping[str, object]:
        del cursor
        snapshot = self._snapshot()
        with self._sessions() as session:
            rollouts = list(
                session.scalars(
                    select(PackageRollout).order_by(
                        PackageRollout.created_at.desc(), PackageRollout.id.desc()
                    )
                )
            )
        latest_rollout: dict[str, str] = {}
        for rollout in rollouts:
            latest_rollout.setdefault(rollout.deployment_id, rollout.id)
        values = []
        for deployment in sorted(self._deployments().values(), key=lambda item: item.deployment_id):
            release_path = (
                "manifests/workload-releases/"
                f"{deployment.family_id}/{deployment.release_digest}.json"
            )
            state = "approved" if release_path in snapshot.documents else "unapproved"
            values.append(
                {
                    "id": deployment.deployment_id,
                    "family_id": deployment.family_id,
                    "release_digest": "sha256:" + deployment.release_digest,
                    "previous_release_digest": None,
                    "state": state,
                    "rollout_id": latest_rollout.get(deployment.deployment_id),
                }
            )
        values = values[:limit]
        return {"deployments": values, "next_cursor": None, "total": len(values)}

    def deployment(self, deployment_id: str) -> Mapping[str, object]:
        values = self.deployments(None, 10_000)["deployments"]
        for value in values:
            if isinstance(value, Mapping) and value.get("id") == deployment_id:
                return value
        raise KeyError(deployment_id)

    def _package_observations(self) -> tuple[Mapping[str, object], ...]:
        """Project authenticated fleet/health/package state for placement."""
        fleet = self._fleet()
        raw_nodes = fleet.get("nodes", ()) if isinstance(fleet, Mapping) else ()
        with self._sessions() as session:
            agent_nodes = {
                node.node_id: node
                for node in session.scalars(select(AgentNode))
            }
            package_rows = list(
                session.scalars(
                    select(PackageObservation).order_by(
                        PackageObservation.observed_at.desc()
                    )
                )
            )
        current_packages: dict[str, dict[str, Mapping[str, object]]] = {}
        seen: set[tuple[str, str]] = set()
        for row in package_rows:
            key = (row.node_id, row.deployment_id)
            if key in seen:
                continue
            seen.add(key)
            current_packages.setdefault(row.node_id, {})[row.deployment_id] = {
                "release_digest": row.release_digest,
                "deployment_digest": (
                    row.summary.get("deployment_digest")
                    if isinstance(row.summary, Mapping)
                    and isinstance(row.summary.get("deployment_digest"), str)
                    else "0" * 64
                ),
            }
        result: list[Mapping[str, object]] = []
        if not isinstance(raw_nodes, (list, tuple)):
            return ()
        for raw in raw_nodes:
            if not isinstance(raw, Mapping):
                continue
            node_id = raw.get("node_id", raw.get("id"))
            if not isinstance(node_id, str):
                continue
            stored = agent_nodes.get(node_id)
            observation = dict(raw)
            observation["node_id"] = node_id
            observation["agent_state"] = raw.get(
                "agent_state", stored.state if stored is not None else "unknown"
            )
            observation["healthy"] = bool(raw.get("healthy", False)) and bool(
                raw.get("agent_online", True)
            )
            observation["capabilities"] = raw.get(
                "capabilities", stored.capabilities if stored is not None else ()
            )
            observation["architecture"] = raw.get(
                "architecture", stored.architecture if stored is not None else None
            )
            observation["operating_system"] = raw.get("operating_system", "linux")
            observation["current_packages"] = current_packages.get(node_id, {})
            result.append(observation)
        return tuple(result)

    @staticmethod
    def _rollout_identity(plan: object, deployment_id: str) -> tuple[str, Mapping[str, object]]:
        graph = getattr(plan, "operation_graph", None)
        releases = getattr(plan, "releases", None)
        payloads = getattr(plan, "operation_payloads", None)
        targets = getattr(plan, "targets", None)
        release = releases.get(deployment_id) if isinstance(releases, Mapping) else None
        if graph is None or not isinstance(release, Mapping) or not isinstance(payloads, Mapping):
            raise RuntimeError("package rollout plan is incomplete")
        document = {
            "schema_version": 1,
            "deployment_id": deployment_id,
            "operation_graph": graph.document,
            "operation_payloads": payloads,
            "release": release,
            "targets": list(targets or ()),
        }
        return hashlib.sha256(canonical_message(document)).hexdigest(), document

    def _resolve_rollout(self, deployment_id: str):
        if self._package_trust is None:
            raise RuntimeError("workload TUF authorization service is not installed")
        resolver = PackageDesiredStateResolver(
            self._repository,
            trust=self._package_trust,
            clock=self._clock,
        )
        return resolver.resolve(
            self._repository.head(),
            (deployment_id,),
            self._package_observations(),
        )

    def rollout_preview(self, deployment_id: str) -> Mapping[str, object]:
        if self._rollouts is None:
            raise RuntimeError("package rollout service is not installed")
        plan = self._resolve_rollout(deployment_id)
        identity, _document = self._rollout_identity(plan, deployment_id)
        release = plan.releases[deployment_id]
        release_digest = release.get("release_digest")
        node_ids = list(plan.placements.get(deployment_id, ()))
        request = {
            "deployment_id": deployment_id,
            "base_commit": plan.commit,
            "plan_identity": identity,
            "release_digest": release_digest,
            "node_ids": sorted(node_ids),
        }
        envelope = release.get("resource_envelope")
        if not isinstance(envelope, Mapping):
            raise TypeError("promoted workload release resource envelope is missing")
        per_node = envelope.get("per_node")
        aggregate = envelope.get("aggregate")
        if not isinstance(per_node, Mapping) or not isinstance(aggregate, Mapping):
            raise TypeError("promoted workload resource envelope is invalid")
        download_bytes = aggregate.get("download_bytes")
        installed_bytes = aggregate.get("installed_bytes")
        transient_bytes = aggregate.get("transient_bytes")
        if not all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in (download_bytes, installed_bytes, transient_bytes)
        ):
            raise TypeError("promoted workload resource envelope is incomplete")
        digest = self.create_action_plan("package.rollout", deployment_id, request)
        return {
            "digest": digest,
            "state": "ready",
            "deployment_id": deployment_id,
            "release_digest": "sha256:" + str(release_digest),
            "batches": [sorted(node_ids[:1]), sorted(node_ids[1:])] if node_ids else [],
            "canary_node": node_ids[0] if node_ids else None,
            "offline_pending": [],
            "storage_bytes": installed_bytes + transient_bytes,
            "download_bytes": download_bytes,
            "resource_envelope": envelope,
        }

    def rollout(
        self, deployment_id: str, plan_digest: str, actor: str, request_id: str
    ) -> Mapping[str, object]:
        if self._rollouts is None:
            raise RuntimeError("package rollout service is not installed")
        replay = self._progress_replay(plan_digest, "package.rollout")
        if replay is not None:
            return replay
        request = self.consume_action_plan(plan_digest, "package.rollout", deployment_id)
        if request.get("deployment_id") != deployment_id:
            raise ValueError("package rollout deployment changed")
        plan = self._resolve_rollout(deployment_id)
        identity, _document = self._rollout_identity(plan, deployment_id)
        if request.get("plan_identity") != identity or request.get("base_commit") != plan.commit:
            raise ValueError("package rollout preview is stale")
        rollout_id = self._rollouts.create(
            plan, deployment_id, actor=actor, request_id=request_id
        )
        self._rollouts.advance(rollout_id)
        result = self.rollout_status(deployment_id, rollout_id, None, 512)
        self.finish_action_plan(plan_digest, result=result)
        return result

    def repair_preview(self, deployment_id: str) -> Mapping[str, object]:
        deployment = self._deployments().get(deployment_id)
        if deployment is None:
            raise KeyError(deployment_id)
        inventory = self.inventory(None, deployment_id, None, 512)
        node_ids = [
            node.get("node_id")
            for node in inventory.get("nodes", [])
            if isinstance(node, Mapping)
            and node.get("online") is True
            and isinstance(node.get("node_id"), str)
        ]
        if not node_ids:
            raise ValueError("package repair has no online target nodes")
        request = {
            "deployment_id": deployment_id,
            "release_digest": deployment.release_digest,
            "node_ids": sorted(node_ids),
            "inventory_digest": _digest(inventory),
        }
        digest = self.create_action_plan("package.repair", deployment_id, request)
        return {
            "digest": digest,
            "state": "ready",
            "deployment_id": deployment_id,
            "release_digest": "sha256:" + deployment.release_digest,
            "batches": [sorted(node_ids)],
            "canary_node": None,
            "offline_pending": [],
            "storage_bytes": 0,
            "download_bytes": 0,
        }

    def repair(
        self, deployment_id: str, plan_digest: str, actor: str, request_id: str
    ) -> Mapping[str, object]:
        replay = self._progress_replay(plan_digest, "package.repair")
        if replay is not None:
            return replay
        request = self.consume_action_plan(plan_digest, "package.repair", deployment_id)
        if request.get("deployment_id") != deployment_id:
            raise ValueError("package repair deployment changed")
        release_digest = request.get("release_digest")
        node_ids = request.get("node_ids")
        if (
            not isinstance(release_digest, str)
            or not isinstance(node_ids, list)
            or any(not isinstance(node_id, str) for node_id in node_ids)
        ):
            raise ValueError("package repair plan is invalid")
        preview = self.repair_preview(deployment_id)
        if preview.get("digest") != plan_digest:
            raise ValueError("package repair preview is stale")
        result = self._queue_package_operations(
            action="package.repair",
            plan_digest=plan_digest,
            request=request,
            actor=actor,
            request_id=request_id,
        )
        self.finish_action_plan(plan_digest, result=result)
        return result

    # ---- Candidate publication -------------------------------------------------

    def promotion_preview(self, candidate_id: str) -> Mapping[str, object]:
        """Create a durable preview for a signer-owned workload publication."""
        if self._publication is None:
            raise RuntimeError("workload publication service is not installed")
        if not isinstance(candidate_id, str) or not candidate_id:
            raise ValueError("package candidate is invalid")
        commit = self._repository.head()
        preview = self._publication.preview(candidate_id, commit)
        preview_digest = getattr(preview, "digest", None)
        release_digest = getattr(preview, "release_digest", None)
        base_commit = getattr(preview, "base_commit", commit)
        if (
            not isinstance(preview_digest, str)
            or _raw_digest(preview_digest) is None
            or not isinstance(release_digest, str)
            or _raw_digest(release_digest) is None
            or base_commit != commit
        ):
            raise RuntimeError("workload publication preview is invalid")
        request = {
            "candidate_id": candidate_id,
            "publication_preview_digest": _raw_digest(preview_digest),
            "release_digest": _raw_digest(release_digest),
            "base_commit": commit,
        }
        digest = self.create_action_plan("package.promote", candidate_id, request)
        return {
            "digest": digest,
            "state": "ready",
            "candidate_id": candidate_id,
            "release_digest": "sha256:" + str(request["release_digest"]),
            "batches": [],
            "canary_node": None,
            "offline_pending": [],
            "storage_bytes": 0,
            "download_bytes": 0,
        }

    def promote(
        self, candidate_id: str, plan_digest: str, actor: str, request_id: str
    ) -> Mapping[str, object]:
        """Apply exactly one preview through the isolated publication service."""
        if self._publication is None:
            raise RuntimeError("workload publication service is not installed")
        replay = self._progress_replay(plan_digest, "package.promote")
        if replay is not None:
            return replay
        request = self.consume_action_plan(plan_digest, "package.promote", candidate_id)
        if request.get("candidate_id") != candidate_id:
            raise ValueError("package promotion candidate changed")
        publication_preview_digest = request.get("publication_preview_digest")
        release_digest = request.get("release_digest")
        base_commit = request.get("base_commit")
        if (
            not isinstance(publication_preview_digest, str)
            or _raw_digest(publication_preview_digest) is None
            or not isinstance(release_digest, str)
            or _raw_digest(release_digest) is None
            or not isinstance(base_commit, str)
            or self._repository.head() != base_commit
        ):
            raise ValueError("package promotion preview is stale")
        target = self._publication.promote(publication_preview_digest, actor)
        target_digest = target.get("digest") if isinstance(target, Mapping) else getattr(target, "digest", None)
        if not isinstance(target_digest, str):
            raise TypeError("workload publication result is invalid")
        target_digest = _raw_digest(target_digest)
        if target_digest != release_digest:
            raise ValueError("workload publication release changed")
        result = {
            "candidate_id": candidate_id,
            "release_digest": "sha256:" + release_digest,
            "digest": "sha256:" + release_digest,
            "state": "published",
        }
        self.finish_action_plan(plan_digest, result=result)
        return result

    def rollout_status(
        self, deployment_id: str, rollout_id: str, cursor: str | None, limit: int
    ) -> Mapping[str, object]:
        del cursor
        with self._sessions() as session:
            rollout = session.get(PackageRollout, rollout_id)
            if rollout is None or rollout.deployment_id != deployment_id:
                raise KeyError(rollout_id)
            nodes = list(
                session.scalars(
                    select(PackageRolloutNode)
                    .where(PackageRolloutNode.rollout_id == rollout.id)
                    .order_by(PackageRolloutNode.batch_index, PackageRolloutNode.node_order)
                )
            )[:limit]
        progress = self._progress(rollout.progress)
        progress["total"] = max(progress["total"], len(nodes))
        progress["completed"] = max(
            progress["completed"], sum(node.state == "accepted" for node in nodes)
        )
        return {
            "id": rollout.id,
            "state": rollout.state,
            "plan_digest": "sha256:" + rollout.plan_digest,
            "progress": progress,
            "failure": rollout.failure_reason,
            "job_id": rollout.job_id,
            "audit_request_id": None,
            "nodes": [
                {
                    "node_id": node.node_id,
                    "state": node.state,
                    "batch_index": node.batch_index,
                    "completed": 1 if node.state == "accepted" else 0,
                    "total": 1,
                }
                for node in nodes
            ],
            "rollback_rollout_id": None,
            "rollback_selector": "retained",
        }

    # ---- Agent-observed per-node inventory ------------------------------------

    @staticmethod
    def _health_resources(payload: object) -> tuple[dict[str, int], dict[str, int]]:
        value = payload if isinstance(payload, Mapping) else {}
        storage = value.get("storage") if isinstance(value.get("storage"), Mapping) else value
        resources = value.get("resources") if isinstance(value.get("resources"), Mapping) else value

        def integer(mapping: object, *names: str) -> int:
            if not isinstance(mapping, Mapping):
                return 0
            for name in names:
                item = mapping.get(name)
                if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
                    return item
            return 0

        total = integer(storage, "total_bytes", "disk_total_bytes", "storage_total_bytes")
        free = integer(storage, "free_bytes", "disk_available_bytes", "storage_available_bytes")
        return (
            {
                "total_bytes": total,
                "used_bytes": max(0, total - free) if total else 0,
                "free_bytes": free,
                "reserved_bytes": integer(storage, "reserved_bytes"),
                "reclaimable_bytes": integer(storage, "reclaimable_bytes"),
            },
            {
                "host_memory_total_bytes": integer(resources, "host_memory_total_bytes", "memory_total_bytes"),
                "host_memory_free_bytes": integer(resources, "host_memory_free_bytes", "memory_available_bytes"),
                "gpu_memory_total_bytes": integer(resources, "gpu_memory_total_bytes"),
                "gpu_memory_free_bytes": integer(resources, "gpu_memory_free_bytes"),
                "gpu_count": integer(resources, "gpu_count"),
            },
        )

    @staticmethod
    def _package_resource_envelope(summary: Mapping[str, object]) -> dict[str, object]:
        """Return only a complete, explicitly declared resource envelope.

        Missing values are deliberately an error.  A zero is meaningful only
        when the signed workload release explicitly declared zero (for
        example, a runtime with no retained output or KV cache).
        """
        raw = summary.get("resources")
        if not isinstance(raw, Mapping):
            raise TypeError("package observation has no resource envelope")
        fields = (
            "download_bytes",
            "installed_bytes",
            "transient_bytes",
            "output_bytes",
            "host_memory_bytes",
            "resident_memory_bytes",
            "auxiliary_memory_bytes",
            "activation_memory_bytes",
            "workspace_memory_bytes",
            "gpu_memory_bytes",
            "gpu_count",
            "cpu_millicores",
            "kv_cache_base_bytes",
            "kv_cache_per_token_bytes",
        )
        result: dict[str, object] = {}
        for field in fields:
            value = raw.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise RuntimeError(f"package resource envelope is missing {field}")
            result[field] = value
        required_nodes = raw.get("required_nodes")
        topology = raw.get("topology")
        if (
            not isinstance(required_nodes, int)
            or isinstance(required_nodes, bool)
            or not 1 <= required_nodes <= 512
            or not isinstance(topology, str)
            or topology not in {"single", "replicated", "gang"}
        ):
            raise RuntimeError("package resource envelope topology is incomplete")
        result["required_nodes"] = required_nodes
        result["topology"] = topology
        for field in ("world_size", "ranks", "fabric"):
            if field not in raw:
                raise RuntimeError(f"package resource envelope is missing {field}")
            result[field] = raw[field]
        return result

    def _signed_package_resource_envelope(
        self,
        family_id: str,
        release_digest: str,
        cache: dict[tuple[str, str], dict[str, object]],
    ) -> dict[str, object]:
        """Project sizing only from the immutable, digest-bound release lock."""
        raw_digest = _raw_digest(release_digest)
        if raw_digest is None or not isinstance(family_id, str) or not family_id:
            raise TypeError("package observation release identity is invalid")
        key = (family_id, raw_digest)
        if key in cache:
            return dict(cache[key])
        path = f"manifests/workload-releases/{family_id}/{raw_digest}.json"
        try:
            document = self._repository.read_document(self._repository.head(), path)
            lock = PackageReleaseLock.parse(document.content)
        except Exception as error:
            raise TypeError("signed package resource envelope is unavailable") from error
        if lock.family_id != family_id or lock.digest != raw_digest:
            raise TypeError("signed package resource envelope identity changed")
        if lock.resource_envelope is None:
            raise TypeError("signed package resource envelope is missing")
        per_node = lock.resource_envelope.get("per_node")
        if not isinstance(per_node, Mapping):
            raise TypeError("signed package resource envelope is invalid")
        envelope = self._package_resource_envelope(
            {
                "resources": {
                    **dict(per_node),
                    "required_nodes": lock.resource_envelope["required_nodes"],
                    "topology": lock.resource_envelope["topology"],
                    "world_size": lock.resource_envelope["world_size"],
                    "ranks": lock.resource_envelope["ranks"],
                    "fabric": lock.resource_envelope["fabric"],
                }
            }
        )
        cache[key] = envelope
        return dict(envelope)

    def inventory(
        self,
        node_id: str | None,
        deployment_id: str | None,
        cursor: str | None,
        limit: int,
    ) -> Mapping[str, object]:
        del cursor
        with self._sessions() as session:
            nodes = list(session.scalars(select(AgentNode).order_by(AgentNode.node_id)))
            health_rows = list(
                session.scalars(
                    select(Observation)
                    .where(Observation.kind == "health")
                    .order_by(Observation.observed_at.desc())
                )
            )
            package_rows = list(
                session.scalars(select(PackageObservation).order_by(PackageObservation.node_id))
            )
        latest_health: dict[str, Observation] = {}
        for row in health_rows:
            latest_health.setdefault(row.node_id, row)
        grouped: dict[str, list[PackageObservation]] = {}
        for row in package_rows:
            if deployment_id is None or row.deployment_id == deployment_id:
                grouped.setdefault(row.node_id, []).append(row)
        deployment_families = {
            deployment.deployment_id: deployment.family_id
            for deployment in self._deployments().values()
        }
        envelope_cache: dict[tuple[str, str], dict[str, object]] = {}
        values = []
        for node in nodes:
            if node_id is not None and node.node_id != node_id:
                continue
            storage, resources = self._health_resources(
                latest_health.get(node.node_id).payload
                if node.node_id in latest_health
                else {}
            )
            packages = []
            current_generation = None
            for row in grouped.get(node.node_id, [])[:2048]:
                summary = _bounded_mapping(row.summary)
                if row.state in {"active", "healthy"}:
                    current_generation = "sha256:" + row.release_digest
                package_state = {
                    "active": "active",
                    "healthy": "active",
                    "stopped": "retained",
                    "prepared": "staged",
                    "failed": "failed",
                }.get(row.state, "available")
                family_id = summary.get("family_id")
                if not isinstance(family_id, str):
                    family_id = deployment_families.get(row.deployment_id)
                if not isinstance(family_id, str):
                    raise TypeError("package observation family identity is missing")
                envelope = self._signed_package_resource_envelope(
                    family_id, row.release_digest, envelope_cache
                )
                packages.append(
                    {
                        "deployment_id": row.deployment_id,
                        "family_id": family_id,
                        "release_digest": "sha256:" + row.release_digest,
                        "content_group": str(summary.get("content_group", "workload")),
                        "state": package_state,
                        "bytes_total": int(summary.get("bytes_total", 0) or 0),
                        "bytes_complete": int(summary.get("bytes_complete", 0) or 0),
                        "bytes_remaining": int(summary.get("bytes_remaining", 0) or 0),
                        "installed_bytes": int(summary.get("installed_bytes", 0) or 0),
                        "reclaimable_bytes": int(summary.get("reclaimable_bytes", 0) or 0),
                        "reserved_bytes": int(summary.get("reserved_bytes", 0) or 0),
                        "active": row.state in {"active", "healthy"},
                        "retained": row.state == "stopped",
                        "leased": False,
                        "operation_id": row.operation_id,
                        "last_operation_state": row.state,
                        "last_operation_error": None,
                        "resources": envelope,
                    }
                )
            values.append(
                {
                    "node_id": node.node_id,
                    "online": node.state == "active" and node.revoked_at is None,
                    "observed_at": _iso(latest_health.get(node.node_id).observed_at)
                    if node.node_id in latest_health
                    else None,
                    "storage": storage,
                    "resources": resources,
                    "current_generation": current_generation,
                    "packages": packages,
                }
            )
        values = values[:limit]
        return {"nodes": values, "next_cursor": None, "total": len(values)}

    def removal_preview(
        self, deployment_id: str, release_digest: str, node_ids: tuple[str, ...]
    ) -> Mapping[str, object]:
        wanted = set(node_ids)
        inventory = self.inventory(None, deployment_id, None, 512)
        rows = []
        for node in inventory.get("nodes", []):
            if not isinstance(node, Mapping) or node.get("node_id") not in wanted:
                continue
            packages = node.get("packages", [])
            package = next(
                (
                    item
                    for item in packages
                    if isinstance(item, Mapping)
                    and item.get("release_digest") == release_digest
                ),
                None,
            )
            active = bool(package and package.get("active"))
            leased = bool(package and package.get("leased"))
            retained = bool(package and package.get("retained"))
            blocked = "active" if active else "leased" if leased else "retained" if retained else None
            rows.append(
                {
                    "node_id": node.get("node_id"),
                    "state": "blocked" if blocked else "removable",
                    "active": active,
                    "retained": retained,
                    "leased": leased,
                    "reclaimable_bytes": int(package.get("reclaimable_bytes", 0)) if package else 0,
                    "dependencies": [],
                    "blocked_reason": blocked,
                }
            )
        normalized_release = _raw_digest(release_digest)
        if normalized_release is None:
            raise ValueError("package release digest is invalid")
        request = {
            "deployment_id": deployment_id,
            "release_digest": normalized_release,
            "node_ids": sorted(wanted),
            "inventory_digest": _digest(rows),
        }
        digest = self.create_action_plan("package.remove", deployment_id, request)
        return {
            "digest": digest,
            "state": "blocked" if any(item["blocked_reason"] for item in rows) else "ready",
            "deployment_id": deployment_id,
            "release_digest": release_digest,
            "nodes": rows,
            "reclaimable_bytes": sum(int(item["reclaimable_bytes"]) for item in rows),
            "blocked_nodes": [item["node_id"] for item in rows if item["blocked_reason"]],
        }

    def _progress_replay(self, digest: str, action: str) -> Mapping[str, object] | None:
        plan = self._action_plan(digest, action)
        if plan.state == "applied" and isinstance(plan.result, Mapping):
            return dict(plan.result)
        return None

    def remove(
        self, plan_digest: str, actor: str, request_id: str
    ) -> Mapping[str, object]:
        replay = self._progress_replay(plan_digest, "package.remove")
        if replay is not None:
            return replay
        request = self.consume_action_plan(plan_digest, "package.remove")
        deployment_id = request.get("deployment_id")
        release_digest = request.get("release_digest")
        node_ids = request.get("node_ids")
        if (
            not isinstance(deployment_id, str)
            or not isinstance(release_digest, str)
            or not isinstance(node_ids, list)
            or any(not isinstance(node_id, str) for node_id in node_ids)
        ):
            raise ValueError("package removal plan is invalid")
        preview = self.removal_preview(
            deployment_id, "sha256:" + release_digest, tuple(node_ids)
        )
        if preview.get("digest") != plan_digest or preview.get("state") == "blocked":
            result = {
                "id": str(uuid.uuid4()),
                "state": "failed",
                "plan_digest": plan_digest,
                "progress": {"completed": 0, "failed": 1, "running": 0, "total": len(node_ids)},
                "failure": "package removal preview is stale or blocked",
                "job_id": None,
                "audit_request_id": request_id,
                "nodes": [],
                "rollback_rollout_id": None,
                "rollback_selector": None,
            }
            self.finish_action_plan(plan_digest, result=result, failed=True)
            raise ValueError("package removal preview is stale or blocked")
        result = self._queue_package_operations(
            action="package.remove",
            plan_digest=plan_digest,
            request=request,
            actor=actor,
            request_id=request_id,
        )
        self.finish_action_plan(plan_digest, result=result)
        return result

    def gc_preview(self) -> Mapping[str, object]:
        inventory = self.inventory(None, None, None, 512)
        node_targets: dict[str, int] = {}
        for node in inventory.get("nodes", []):
            if not isinstance(node, Mapping) or not node.get("online"):
                continue
            storage = node.get("storage")
            if not isinstance(storage, Mapping):
                continue
            reclaimable = storage.get("reclaimable_bytes", 0)
            if isinstance(reclaimable, int) and reclaimable > 0:
                node_id = node.get("node_id")
                if isinstance(node_id, str):
                    node_targets[node_id] = reclaimable
        request = {
            "node_ids": sorted(node_targets),
            "target_bytes_by_node": node_targets,
            "target_bytes": sum(node_targets.values()),
        }
        digest = self.create_action_plan("package.gc", "cluster", request)
        return {
            "digest": digest,
            "state": "ready" if node_targets else "empty",
            "storage_bytes": request["target_bytes"],
            "download_bytes": 0,
            "reclaim_bytes": request["target_bytes"],
        }

    def gc(self, plan_digest: str, actor: str, request_id: str) -> Mapping[str, object]:
        replay = self._progress_replay(plan_digest, "package.gc")
        if replay is not None:
            return replay
        request = self.consume_action_plan(plan_digest, "package.gc")
        node_ids = request.get("node_ids")
        target_bytes = request.get("target_bytes", 0)
        if not isinstance(node_ids, list) or any(not isinstance(item, str) for item in node_ids):
            raise ValueError("package GC plan is invalid")
        if not isinstance(target_bytes, int) or target_bytes < 0:
            raise ValueError("package GC target is invalid")
        if not node_ids or target_bytes == 0:
            result = {
                "id": str(uuid.uuid4()),
                "state": "succeeded",
                "plan_digest": plan_digest,
                "progress": {"completed": 0, "failed": 0, "running": 0, "total": 0},
                "failure": None,
                "job_id": None,
                "audit_request_id": request_id,
                "nodes": [],
                "rollback_rollout_id": None,
                "rollback_selector": None,
            }
            self.finish_action_plan(plan_digest, result=result)
            return result
        result = self._queue_package_operations(
            action="package.gc",
            plan_digest=plan_digest,
            request=request,
            actor=actor,
            request_id=request_id,
        )
        self.finish_action_plan(plan_digest, result=result)
        return result

    # ---- Durable validation boundary -------------------------------------------

    def _validation_inputs(
        self, candidate_id: str
    ) -> tuple[dict[str, object], str, str, object, WorkloadDeployment]:
        """Load one candidate, exact lock, policy, and resolution binding.

        The lock is parsed from the eligible Git document on every preview and
        apply.  SQL contributes only the candidate/resolution projection; it
        never supplies release bytes or execution policy.
        """
        commit = self._repository.head()
        with self._sessions() as session:
            candidate = self._candidate_row(session, candidate_id)
            resolution = self._resolution_row(session, candidate_id)
            if resolution.state != "resolved" or not isinstance(resolution.release_digest, str):
                raise ValidationError("candidate resolution is not resolved")
            summary = _bounded_mapping(candidate.summary)
            resolution_id = resolution.id
            release_digest = resolution.release_digest
        path = f"manifests/workload-releases/{candidate.family_id}/{release_digest}.json"
        document = self._repository.read_document(commit, path)
        try:
            lock = PackageReleaseLock.parse(document.parsed)
        except Exception as error:
            raise ValidationError("candidate release lock is invalid") from error
        if lock.family_id != candidate.family_id or lock.digest != release_digest:
            raise ValidationError("candidate release lock identity changed")
        if document.content not in {lock.canonical_bytes, lock.canonical_bytes + b"\n"}:
            raise ValidationError("candidate release lock formatting changed")
        family = self._families().get(candidate.family_id)
        if family is None:
            raise ValidationError("candidate package family is unavailable")
        validation_deployment_id = family.validation_deployment_id
        if validation_deployment_id is None:
            raise ValidationError("validation-deployment-missing")
        deployment_path = (
            f"config/workload-deployments/{validation_deployment_id}.toml"
        )
        deployment_document = self._repository.read_document(commit, deployment_path)
        try:
            deployment = WorkloadDeployment.load(deployment_document.parsed)
        except Exception as error:
            raise ValidationError("validation deployment is invalid") from error
        if (
            deployment.deployment_id != validation_deployment_id
            or deployment.family_id != candidate.family_id
            or deployment.release_digest != release_digest
        ):
            raise ValidationError("validation deployment release binding changed")
        deployment_digest = hashlib.sha256(deployment.canonical_bytes).hexdigest()
        policy = dict(family.policy)
        # Keep the family policy authoritative, while carrying the family
        # validation suite as a bounded requirement for the runner.
        policy["validation"] = [dict(item) for item in family.validation]
        evidence = summary.get("evidence")
        candidate_value: dict[str, object] = {
            "id": candidate.id,
            "family_id": candidate.family_id,
            "state": candidate.state,
            "lock": lock,
            "policy": policy,
            "signature_verified": True,
            "provenance_verified": True,
            "license_accepted": summary.get("license_accepted", True),
            "evidence": dict(evidence) if isinstance(evidence, Mapping) else {},
            "deployment": json.loads(deployment.canonical_bytes),
            "deployment_digest": deployment_digest,
            "deployment_config_digest": deployment_digest,
        }
        return candidate_value, commit, resolution_id, lock, deployment

    def _validation_fleet(self) -> object:
        """Return an authenticated, bounded fleet projection for compatibility."""
        value = self._fleet()
        if not isinstance(value, Mapping):
            return value
        raw_nodes = value.get("nodes")
        if not isinstance(raw_nodes, (list, tuple)):
            return value
        with self._sessions() as session:
            stored = {
                node.node_id: node
                for node in session.scalars(select(AgentNode))
            }
        nodes: list[dict[str, object]] = []
        for raw in raw_nodes:
            if not isinstance(raw, Mapping):
                continue
            node = dict(raw)
            node_id = node.get("node_id", node.get("id"))
            if not isinstance(node_id, str):
                continue
            node["node_id"] = node_id
            stored_node = stored.get(node_id)
            if "capabilities" not in node and stored_node is not None:
                node["capabilities"] = list(stored_node.capabilities or ())
            if "architecture" not in node and stored_node is not None:
                node["architecture"] = stored_node.architecture
            node.setdefault("authenticated", node.get("agent_online") is True)
            node.setdefault("online", node.get("agent_online") is True)
            nodes.append(node)
        return {"nodes": nodes}

    def _validation_controller(self, candidate: Mapping[str, object]) -> ValidationController:
        return ValidationController(
            candidate_loader=lambda _candidate_id: candidate,
            fleet_loader=self._validation_fleet,
            clock=self._clock,
        )

    def _validation_response(
        self,
        run: PackageValidationRun,
        *,
        plan_digest: str,
        failure: str | None = None,
    ) -> Mapping[str, object]:
        progress = ProductionPackageProjectionService._progress(run.progress)
        if progress["total"] == 0:
            progress["total"] = 1
        job_id: str | None = None
        nodes: list[dict[str, object]] = []
        with self._sessions() as session:
            job = session.scalar(
                select(Job).where(
                    Job.request_id == run.id,
                    Job.kind == "package.validation",
                )
            )
            if job is not None:
                job_id = job.id
                grouped: dict[str, dict[str, object]] = {}
                operations = session.scalars(
                    select(StoredAgentOperation)
                    .where(StoredAgentOperation.parent_job_id == job.id)
                    .order_by(StoredAgentOperation.created_at, StoredAgentOperation.id)
                )
                for operation in operations:
                    item = grouped.setdefault(
                        operation.node_id,
                        {
                            "node_id": operation.node_id,
                            "state": "queued",
                            "batch_index": 0,
                            "completed": 0,
                            "total": 0,
                        },
                    )
                    item["total"] = int(item["total"]) + 1
                    if operation.state in {"succeeded", "compensated"}:
                        item["completed"] = int(item["completed"]) + 1
                    current = str(item["state"])
                    if operation.state in {"failed", "waiting-for-operator"}:
                        item["state"] = "failed"
                    elif current != "failed" and operation.state == "running":
                        item["state"] = "running"
                for item in grouped.values():
                    if item["state"] == "queued" and item["completed"] == item["total"]:
                        item["state"] = "succeeded"
                nodes = list(grouped.values())[:512]
        return {
            "id": run.id,
            "state": run.state,
            "plan_digest": "sha256:" + _raw_digest(plan_digest),
            "progress": progress,
            "failure": failure if failure is not None else run.reason_code,
            "job_id": job_id,
            "audit_request_id": run.id,
            "nodes": nodes,
            "rollback_rollout_id": None,
            "rollback_selector": None,
        }

    def validation_preview(self, candidate_id: str) -> Mapping[str, object]:
        candidate, commit, resolution_id, lock, _deployment = self._validation_inputs(candidate_id)
        controller = self._validation_controller(candidate)
        plan = controller.plan(candidate_id)
        now = self._clock()
        with self._sessions.begin() as session:
            run = session.scalar(
                select(PackageValidationRun)
                .where(
                    PackageValidationRun.candidate_id == candidate_id,
                    PackageValidationRun.resolution_id == resolution_id,
                    PackageValidationRun.validation_kind == "artifact",
                    PackageValidationRun.release_digest == lock.digest,
                    PackageValidationRun.policy_digest == plan.policy_digest,
                    PackageValidationRun.fleet_digest == plan.compatibility_digest,
                )
                .order_by(PackageValidationRun.updated_at.desc(), PackageValidationRun.id.desc())
            )
            if run is None or run.state in {"failed", "rejected", "cancelled"}:
                run = PackageValidationRun(
                    id=str(uuid.uuid4()),
                    candidate_id=candidate_id,
                    resolution_id=resolution_id,
                    validation_kind="artifact",
                    release_digest=lock.digest,
                    policy_digest=plan.policy_digest,
                    fleet_digest=plan.compatibility_digest,
                    state="planned",
                    attempt=0,
                    actor="package-validation",
                    progress={
                        "completed": 0,
                        "failed": 0,
                        "running": 0,
                        "total": len(plan.operations),
                    },
                    created_at=now,
                    updated_at=now,
                )
                session.add(run)
            run_id = run.id
        request = {
            "candidate_id": candidate_id,
            "validation_id": run_id,
            "validation_plan_digest": plan.digest,
            "release_digest": lock.digest,
            "base_commit": commit,
            "policy_digest": plan.policy_digest,
            "fleet_digest": plan.compatibility_digest,
            "node_ids": list(plan.node_ids),
            "operations": [dict(operation) for operation in plan.operations],
            "required_evidence": list(
                dict.fromkeys(
                    [
                        *(
                            item
                            for item in candidate.get("policy", {}).get(
                                "required_evidence", []
                            )
                            if isinstance(item, str)
                        ),
                        *(
                            item.get("kind")
                            for item in lock.validation
                            if isinstance(item, Mapping)
                            and item.get("required") is True
                            and isinstance(item.get("kind"), str)
                        ),
                    ]
                )
            ),
        }
        digest = self.create_action_plan("package.validate", candidate_id, request)
        return {
            "digest": digest,
            "state": "ready",
            "candidate_id": candidate_id,
            "release_digest": "sha256:" + lock.digest,
            "validation_id": run_id,
            "batches": [list(plan.node_ids)],
            "canary_node": plan.node_ids[0] if plan.node_ids else None,
            "offline_pending": [],
            "storage_bytes": 0,
            "download_bytes": 0,
        }

    def validate(
        self, candidate_id: str, plan_digest: str, actor: str, request_id: str
    ) -> Mapping[str, object]:
        del request_id
        replay = self._progress_replay(plan_digest, "package.validate")
        if replay is not None:
            return replay
        request = self.consume_action_plan(plan_digest, "package.validate", candidate_id)
        if request.get("candidate_id") != candidate_id:
            raise ValueError("package validation candidate changed")
        candidate, commit, _resolution_id, lock, _deployment = self._validation_inputs(candidate_id)
        controller = self._validation_controller(candidate)
        plan = controller.plan(candidate_id)
        if (
            request.get("validation_plan_digest") != plan.digest
            or request.get("base_commit") != commit
            or request.get("release_digest") != lock.digest
        ):
            raise ValueError("package validation preview is stale")
        validation_id = request.get("validation_id")
        if not isinstance(validation_id, str):
            raise TypeError("package validation identity is invalid")
        with self._sessions.begin() as session:
            run = session.get(PackageValidationRun, validation_id)
            if run is None or run.candidate_id != candidate_id or run.release_digest != lock.digest:
                raise ValueError("package validation run is invalid")
            run.actor = actor
            run.state = "running"
            run.attempt += 1
            run.started_at = run.started_at or self._clock()
            run.updated_at = self._clock()
            run.progress = {
                "completed": 0,
                "failed": 0,
                "running": len(plan.operations),
                "total": len(plan.operations),
            }
        if self._validation_runner is None:
            with self._sessions.begin() as session:
                run = session.get(PackageValidationRun, validation_id)
                assert run is not None
                run.state = "retryable"
                run.reason_code = "validation-runner-unavailable"
                run.progress = {
                    "completed": 0,
                    "failed": 0,
                    "running": 0,
                    "total": len(plan.operations),
                }
                run.updated_at = self._clock()
                result = self._validation_response(run, plan_digest=plan.digest)
            self.finish_action_plan(plan_digest, result=result, failed=True)
            raise RuntimeError("workload validation runner is not installed")
        runner = self._validation_runner
        try:
            if callable(runner):
                outcome = runner(dict(request))
            else:
                outcome = runner.run(dict(request))
        except (OSError, RuntimeError, TimeoutError, ConnectionError) as error:
            outcome = {"status": "retryable", "reason_code": "validation-runner-failed"}
            del error
        if not isinstance(outcome, Mapping):
            raise TypeError("package validation runner result is invalid")
        status = outcome.get("status")
        if status not in {"running", "passed", "failed", "retryable", "rejected"}:
            raise ValueError("package validation runner status is invalid")
        evidence = outcome.get("evidence", {})
        if not isinstance(evidence, Mapping) or len(canonical_message(evidence)) > 16_384:
            raise ValueError("package validation runner evidence is invalid")
        required_evidence = candidate.get("policy", {}).get("required_evidence", ())
        required_validation = [
            item.get("kind")
            for item in lock.validation
            if isinstance(item, Mapping) and item.get("required") is True
        ]
        missing_evidence = [
            str(kind)
            for kind in (*required_evidence, *required_validation)
            if isinstance(kind, str) and kind not in evidence
        ]
        if status == "passed" and missing_evidence:
            status = "failed"
            outcome = {
                "status": "failed",
                "reason_code": "validation-evidence-missing",
                "evidence": {},
            }
            evidence = {}
        with self._sessions.begin() as session:
            run = session.get(PackageValidationRun, validation_id)
            assert run is not None
            run.state = str(status)
            run.reason_code = (
                str(outcome.get("reason_code"))[:80]
                if isinstance(outcome.get("reason_code"), str)
                else None
            )
            run.evidence = dict(evidence) if status == "passed" else None
            run.failure_detail = None
            run.progress = {
                "completed": len(plan.operations) if status == "passed" else 0,
                "failed": 1 if status in {"failed", "rejected"} else 0,
                "running": 0 if status != "running" else len(plan.operations),
                "total": len(plan.operations),
            }
            run.completed_at = self._clock() if status in {"passed", "failed", "rejected"} else None
            run.updated_at = self._clock()
            result = self._validation_response(run, plan_digest=plan.digest)
        self.finish_action_plan(plan_digest, result=result, failed=status in {"failed", "rejected"})
        return result

    # ---- Durable retained-release rollback boundary ---------------------------

    def _rollback_inputs(self, deployment_id: str, rollout_id: str) -> tuple[PackageRollout, tuple[str, ...], str, str | None]:
        with self._sessions() as session:
            rollout = session.get(PackageRollout, rollout_id)
            if rollout is None or rollout.deployment_id != deployment_id:
                raise KeyError(rollout_id)
            if not rollout.previous_release_digest:
                raise ValueError("package rollout has no retained predecessor")
            if rollout.state in {"planned", "preparing", "activating", "health-checking", "soaking", "rolling-back"}:
                raise ValueError("package rollout is still active")
            nodes = tuple(
                node.node_id
                for node in session.scalars(
                    select(PackageRolloutNode)
                    .where(PackageRolloutNode.rollout_id == rollout_id)
                    .order_by(PackageRolloutNode.batch_index, PackageRolloutNode.node_order)
                )
            )
        if not nodes:
            raise ValueError("package rollout has no retained nodes")
        previous_deployment_digest = None
        plan = rollout.plan if isinstance(rollout.plan, Mapping) else {}
        release = plan.get("release") if isinstance(plan, Mapping) else None
        if isinstance(release, Mapping) and isinstance(release.get("previous_deployment_digest"), str):
            previous_deployment_digest = release["previous_deployment_digest"]
        return rollout, nodes, rollout.previous_release_digest, previous_deployment_digest

    def rollback_preview(self, deployment_id: str, rollout_id: str) -> Mapping[str, object]:
        rollout, nodes, release_digest, deployment_digest = self._rollback_inputs(
            deployment_id, rollout_id
        )
        request = {
            "deployment_id": deployment_id,
            "rollout_id": rollout_id,
            "release_digest": release_digest,
            "deployment_digest": deployment_digest,
            "node_ids": list(nodes),
            "base_commit": rollout.base_commit,
            "rollout_plan_digest": rollout.plan_digest,
        }
        digest = self.create_action_plan("package.rollback", deployment_id, request)
        return {
            "digest": digest,
            "state": "ready",
            "deployment_id": deployment_id,
            "release_digest": "sha256:" + release_digest,
            "batches": [list(nodes)],
            "canary_node": None,
            "offline_pending": [],
            "storage_bytes": 0,
            "download_bytes": 0,
        }

    def rollback(
        self,
        deployment_id: str,
        rollout_id: str,
        plan_digest: str,
        actor: str,
        request_id: str,
    ) -> Mapping[str, object]:
        replay = self._progress_replay(plan_digest, "package.rollback")
        if replay is not None:
            return replay
        request = self.consume_action_plan(plan_digest, "package.rollback", deployment_id)
        if request.get("rollout_id") != rollout_id or request.get("deployment_id") != deployment_id:
            raise ValueError("package rollback identity changed")
        rollout, nodes, release_digest, deployment_digest = self._rollback_inputs(
            deployment_id, rollout_id
        )
        if (
            request.get("release_digest") != release_digest
            or request.get("node_ids") != list(nodes)
            or request.get("base_commit") != rollout.base_commit
            or request.get("rollout_plan_digest") != rollout.plan_digest
            or (deployment_digest is not None and request.get("deployment_digest") != deployment_digest)
            or self._repository.head() != rollout.base_commit
        ):
            raise ValueError("package rollback preview is stale")
        result = self._queue_package_operations(
            action="package.rollback",
            plan_digest=plan_digest,
            request=request,
            actor=actor,
            request_id=request_id,
        )
        result = dict(result)
        result["rollback_rollout_id"] = rollout_id
        result["rollback_selector"] = "retained"
        with self._sessions.begin() as session:
            stored = session.get(PackageRollout, rollout_id)
            if stored is not None:
                stored.state = "rolling-back"
                stored.updated_at = self._clock()
        self.finish_action_plan(plan_digest, result=result)
        return result

    def idempotency(
        self,
        actor: str,
        request_id: str,
        fingerprint: tuple[object, ...],
        call: Callable[[], Mapping[str, object]],
    ) -> tuple[Mapping[str, object], bool]:
        key = (actor, request_id, *fingerprint)
        with self._idempotency_lock:
            if key in self._idempotent:
                return self._idempotent[key], True
            result = dict(call())
            self._idempotent[key] = result
            return result, False


__all__ = ["ProductionPackageProjectionService"]
