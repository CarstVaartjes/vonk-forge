"""Controller-owned rolling upgrades for enrolled Spark agents."""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx2
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    AgentResult,
    InvalidRequestError,
    InvalidRequestReason,
    LifecycleState,
    SecurityRefusalError,
    SecurityRefusalReason,
    UnknownOutcomeError,
    WaitReason,
    canonical_message,
)
from vonk_agent_protocol.claims import AGENT_PROTOCOL_VERSION
from vonk_agent_protocol.package_source import AgentPackageSource

from . import agent_operation_states, job_states
from .agent_jobs import (
    AGENT_UPGRADE_RECOVERY_FENCE,
    AgentJobService,
    agent_upgrade_in_flight,
    schedule_agent_upgrade_retry,
)
from .agent_package_source import load_package_source
from .bounded_json import require_integer
from .bounded_retry import bounded_attempts
from .categorized_errors import (
    InvalidType,
    InvalidValue,
    MissingRecord,
)
from .lifecycle import CancelRequested, Outcome, Reported
from .lifecycle.agent_operation import AgentOperationAdapter
from .lifecycle.agent_upgrade import UNSUPPORTED_DISPATCH, AgentUpgradeAdapter
from .lifecycle.evidence import BookkeepingReason, retire_as_unknown
from .models import AgentNode, AgentOperation, AgentOperationAttempt, Job, JobAttempt
from .strict_json import read_stored_model

_PACKAGE_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+~-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_BUILD_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SIGNATURE = re.compile(r"[0-9a-f]{128}\Z")
_NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")
_ONLINE_WINDOW = timedelta(seconds=150)
# An ambiguous install result can leave durable apt/dpkg recovery in progress.
# Every automatic retry waits through this controller safety window.
_AGENT_UPGRADE_RECOVERY_FENCE = AGENT_UPGRADE_RECOVERY_FENCE
_ACTIVE_ROLLOUT_STATES = job_states.words(
    LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.NEEDS_OPERATOR
)
_ALREADY_CURRENT = "already runs the requested agent build"


def _summary(skipped: Mapping[str, str]) -> str | None:
    """Explain what a concluded rollout left untouched, never inventing work."""

    if not skipped:
        return None
    if all(reason == _ALREADY_CURRENT for reason in skipped.values()):
        return "selected Sparks already running the requested agent build were left unchanged"
    passed = sorted(
        (node_id, reason)
        for node_id, reason in skipped.items()
        if reason != _ALREADY_CURRENT
    )
    listed = "; ".join(f"Spark {node_id} {reason}" for node_id, reason in passed[:4])
    more = f" (+{len(passed) - 4} more)" if len(passed) > 4 else ""
    return f"skipped: {listed}{more}"[:1024]


def _failure_detail(result: object) -> str | None:
    """The failed attempt's own explanation, for the operator-visible reason."""

    if not isinstance(result, Mapping):
        return None
    parts: list[str] = []
    for key in ("reason", "error_code"):
        value = result.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
            break
    for key in ("helper_error_code", "diagnostic"):
        value = result.get(key)
        if isinstance(value, str) and value and value not in " ".join(parts):
            parts.append(f"{key}={value}")
    exit_code = result.get("helper_exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        parts.append(f"helper_exit_code={exit_code}")
    return "; ".join(parts)[:256] or None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _request_intent(
    value: object,
    node_ids: Sequence[str] | None,
) -> dict[str, object]:
    if value is None:
        return {
            "all": node_ids is None,
            "selectors": None if node_ids is None else list(node_ids),
        }
    if not isinstance(value, Mapping):
        raise AgentUpgradeInvalid(
            "agent upgrade request intent is invalid",
            reason=InvalidRequestReason.MALFORMED,
        )
    if set(value) != {"all", "selectors"} or type(value.get("all")) is not bool:
        raise AgentUpgradeInvalid(
            "agent upgrade request intent is invalid",
            reason=InvalidRequestReason.MALFORMED,
        )
    all_nodes = value["all"]
    selectors = value["selectors"]
    if all_nodes:
        if selectors is not None:
            raise AgentUpgradeInvalid(
                "agent upgrade request intent is invalid",
                reason=InvalidRequestReason.MALFORMED,
            )
        return {"all": True, "selectors": None}
    if (
        not isinstance(selectors, list)
        or not selectors
        or len(selectors) > 64
        or not all(isinstance(selector, str) and selector for selector in selectors)
    ):
        raise AgentUpgradeInvalid(
            "agent upgrade request intent is invalid",
            reason=InvalidRequestReason.MALFORMED,
        )
    return {"all": False, "selectors": list(selectors)}


def _conflict_detail(detail: str, spark_id: str | None) -> str:
    if spark_id is None:
        return detail
    return (
        f"Spark {spark_id} {detail}"
        if _NODE_ID.fullmatch(spark_id) is not None
        else "agent upgrade target is invalid"
    )


class AgentUpgradeConflict(RuntimeError):
    """An agent upgrade plan is invalid, stale, or not safely executable.

    The refusal text is surfaced to an operator, so a Spark is named only by its
    canonical identifier.  Any other ``spark_id`` is a stored row value and is
    replaced rather than echoed.  Raise one of the categorized subclasses.
    """

    def __init__(self, detail: str, *, spark_id: str | None = None) -> None:
        super().__init__(_conflict_detail(detail, spark_id))


class AgentUpgradeInvalid(InvalidRequestError, AgentUpgradeConflict):
    """The request, plan or manifest is malformed or conflicts with the request."""

    def __init__(
        self,
        detail: str,
        *,
        spark_id: str | None = None,
        reason: InvalidRequestReason | None = None,
    ) -> None:
        InvalidRequestError.__init__(
            self, _conflict_detail(detail, spark_id), reason=reason
        )


#: Pause before the second and third release fetch (seconds): a release is
#: published in a few steps, so a half-published channel settles within them.
_RELEASE_PAUSES = (0.5, 2.0)


class AgentUpgradeUnavailable(UnknownOutcomeError, AgentUpgradeConflict):
    """Release or stored evidence is unavailable or moved: observe and retry."""

    def __init__(
        self,
        detail: str,
        *,
        spark_id: str | None = None,
        reason: WaitReason | None = None,
    ) -> None:
        UnknownOutcomeError.__init__(
            self, _conflict_detail(detail, spark_id), reason=reason
        )


class AgentUpgradeRefused(SecurityRefusalError, AgentUpgradeConflict):
    """The release evidence fails its identity or signature checks."""

    def __init__(
        self,
        detail: str,
        *,
        spark_id: str | None = None,
        reason: SecurityRefusalReason | None = None,
    ) -> None:
        SecurityRefusalError.__init__(
            self, _conflict_detail(detail, spark_id), reason=reason
        )


class AgentUpgradeRetryLater(UnknownOutcomeError, AgentUpgradeConflict):
    """The release channel did not answer consistently (a release is being
    published).  Nothing was persisted; the caller asks again and converges."""

    def __init__(
        self,
        detail: str,
        *,
        spark_id: str | None = None,
        reason: WaitReason | None = WaitReason.OBSERVATION_UNAVAILABLE,
    ) -> None:
        UnknownOutcomeError.__init__(
            self, _conflict_detail(detail, spark_id), reason=reason
        )


@dataclass(frozen=True, slots=True)
class AgentUpgradePlan:
    authority_revision: str
    node_ids: tuple[str, ...]
    package: dict[str, object]
    plan_digest: str
    repair_manifest: dict[str, object] | None
    request_intent: dict[str, object]
    sources: dict[str, dict[str, object]]
    #: Selected Sparks this rollout will not touch, with the reason.  A Spark
    #: that already runs the target is a no-op, not a conflict; one that can
    #: never take this package is reported instead of refusing the fleet.
    skipped: dict[str, str]


class AgentUpgradeService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        operations: AgentJobService,
        *,
        clock: Callable[[], datetime],
        channel: str = "dev",
        release_api_url: str = "https://install.vonkforge.ai",
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self._sessions = sessions
        self._operations = operations
        self._clock = clock
        self._rollouts = AgentUpgradeAdapter()
        operations.set_rollout_owner(
            self._advance, self.advance_node, reconcile=self.heal_rollouts
        )
        if channel not in {"dev", "stable"}:
            raise InvalidValue(
                "agent upgrade channel is invalid",
                reason=InvalidRequestReason.MALFORMED,
            )
        self._channel = channel
        self._http = httpx2.Client(
            base_url=release_api_url,
            follow_redirects=False,
            timeout=httpx2.Timeout(15.0, connect=5.0),
            trust_env=False,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def current_package(self) -> dict[str, object]:
        """The signed current release; a channel that is mid-publication is asked
        again a few times before the requester is told to retry."""

        refused: AgentUpgradeUnavailable | AgentUpgradeRetryLater | None = None
        for _attempt in bounded_attempts(_RELEASE_PAUSES):
            try:
                return self._current_package_once()
            except (AgentUpgradeUnavailable, AgentUpgradeRetryLater) as error:
                refused = error
        assert refused is not None
        raise refused

    def _current_package_once(self) -> dict[str, object]:
        prefix = f"/artifacts/{self._channel}"
        try:
            manifest_response = self._http.get(f"{prefix}/current.manifest")
            manifest_response.raise_for_status()
            if len(manifest_response.content) > 64 * 1024:
                raise AgentUpgradeUnavailable(
                    "agent release manifest is too large",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            manifest = dict(
                line.split("=", 1)
                for line in manifest_response.text.splitlines()
                if "=" in line
            )
            release_path = manifest.get("release_path", "")
            generation = manifest.get("generation", "")
            if (
                _SHA256.fullmatch(generation) is None
                or release_path
                != f"artifacts/{self._channel}/releases/{generation}/release.json"
            ):
                raise AgentUpgradeUnavailable(
                    "agent release manifest is invalid",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            release_response = self._http.get(f"/{release_path}")
            release_response.raise_for_status()
            if len(release_response.content) > 256 * 1024:
                raise AgentUpgradeUnavailable(
                    "agent release document is too large",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            release = release_response.json()
            artifact = release["artifacts"]["agent-package-linux-arm64"]
            signature_record = release["artifacts"][
                "agent-package-signature-linux-arm64"
            ]
            if not isinstance(artifact, Mapping) or not isinstance(
                signature_record, Mapping
            ):
                raise InvalidType(
                    "agent release artifact record is not an object",
                    reason=InvalidRequestReason.MALFORMED,
                )
            signature_response = self._http.get(f"/{signature_record['path']}")
            signature_response.raise_for_status()
            signature = signature_response.text.strip()
        except (httpx2.HTTPError, KeyError, TypeError, ValueError) as error:
            # Name what could not be resolved and what to do about it; the
            # operator cannot see the release relay from the CLI.
            raise AgentUpgradeUnavailable(
                f"the current {self._channel} agent package could not be resolved "
                f"from the release channel ({type(error).__name__}); check that the "
                "Controller reaches install.vonkforge.ai and that it serves a "
                "complete release, then retry",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            ) from error
        if (
            release.get("channel") != self._channel
            or release.get("generation") != generation
            or artifact.get("host_signature") != signature
            or signature_record.get("sha256")
            != hashlib.sha256(signature_response.content).hexdigest()
            or signature_record.get("size") != len(signature_response.content)
        ):
            raise AgentUpgradeRetryLater("current agent release is inconsistent")
        return self._package(
            {
                "architecture": artifact.get("architecture"),
                "package_bytes": artifact.get("size"),
                "package_sha256": artifact.get("sha256"),
                "package_signature": signature,
                "package_url": f"https://install.vonkforge.ai/{artifact.get('path')}",
                "package_version": artifact.get("package_version"),
                "schema_version": 1,
                "target_binary_digest": artifact.get("target_binary_digest"),
                "target_build_digest": artifact.get("target_build_digest"),
            }
        )

    def preview(
        self,
        node_ids: Sequence[str] | None,
        package: Mapping[str, object],
        *,
        repair_manifest: Mapping[str, object] | None = None,
        request_intent: Mapping[str, object] | None = None,
    ) -> AgentUpgradePlan:
        payload = self._package(package)
        intent = _request_intent(request_intent, node_ids)
        repair = (
            None
            if repair_manifest is None
            else self._repair_manifest(repair_manifest, payload)
        )
        if repair is not None and (
            node_ids is None or tuple(node_ids) != (repair["node_id"],)
        ):
            raise AgentUpgradeInvalid(
                "agent repair requires exactly its explicit Spark",
                reason=InvalidRequestReason.INCOMPLETE,
            )
        # The agent wire requires a 64-hex authority revision on every
        # upgrade operation; the signed package digest names what it installs.
        authority_revision = str(payload["package_sha256"])
        requested = None if node_ids is None else tuple(node_ids)
        if requested is not None and (
            not requested
            or len(requested) != len(set(requested))
            or len(requested) > 64
        ):
            raise AgentUpgradeInvalid(
                "agent upgrade targets are invalid",
                reason=InvalidRequestReason.MALFORMED,
            )
        skipped: dict[str, str] = {}
        installed: list[tuple[str, str, str]] = []
        with self._sessions() as session:
            candidates = {
                node.node_id: node
                for node in session.scalars(
                    select(AgentNode)
                    if requested is None
                    else select(AgentNode).where(AgentNode.node_id.in_(requested))
                )
            }
            for node_id in sorted(candidates) if requested is None else requested:
                node = candidates.get(node_id)
                if node is None:
                    skipped[node_id] = "does not exist"
                elif self._at_target(node, payload):
                    skipped[node_id] = _ALREADY_CURRENT
                elif (
                    reason := self._permanent_ineligible_reason(node, payload)
                ) is not None:
                    skipped[node_id] = reason
                else:
                    # An offline Spark stays in the rollout: it is deferred and
                    # upgraded automatically when it reconnects.
                    installed.append(
                        (node_id, node.build_digest or "", node.binary_digest or "")
                    )
        sources: dict[str, dict[str, object]] = {}
        for node_id, build_digest, binary_digest in installed:
            try:
                source = load_package_source(
                    self._http, self._channel, build_digest, binary_digest
                )
            except (httpx2.HTTPError, ValueError):
                skipped[node_id] = "has no published signed rollback package"
                continue
            sources[node_id] = source.model_dump(mode="json")
        targets = tuple(node_id for node_id, _, _ in installed if node_id in sources)
        document = {
            "sources": sources,
            "authority_revision": authority_revision,
            "node_ids": list(targets),
            "package": payload,
            "request_intent": intent,
            **({"repair_manifest": repair} if repair is not None else {}),
        }
        return AgentUpgradePlan(
            authority_revision=authority_revision,
            node_ids=targets,
            package=payload,
            plan_digest=hashlib.sha256(canonical_message(document)).hexdigest(),
            repair_manifest=repair,
            request_intent=intent,
            sources=sources,
            skipped=skipped,
        )

    def get_request(
        self,
        request_id: str,
        *,
        actor: str,
        request_intent: Mapping[str, object],
    ) -> Job | None:
        """Return the exact durable job for a repeated fleet-upgrade request."""

        intent = _request_intent(request_intent, None)
        with self._sessions() as session:
            job = session.scalar(select(Job).where(Job.request_id == request_id))
            if job is None:
                return None
            self._check_replayed_request(job, actor, intent)
            session.expunge(job)
            return job

    @staticmethod
    def _check_replayed_request(
        job: Job, actor: str, request_intent: Mapping[str, object]
    ) -> None:
        if (
            job.kind != "agent-upgrade"
            or job.actor != actor
            or not isinstance(job.payload, Mapping)
            or "request_intent" not in job.payload
        ):
            raise AgentUpgradeInvalid(
                "agent upgrade request key was already used",
                reason=InvalidRequestReason.CONFLICT,
            )
        try:
            stored_intent = _request_intent(job.payload.get("request_intent"), None)
        except AgentUpgradeConflict:
            raise AgentUpgradeInvalid(
                "agent upgrade request key was already used differently",
                reason=InvalidRequestReason.CONFLICT,
            ) from None
        if stored_intent != request_intent:
            raise AgentUpgradeInvalid(
                "agent upgrade request key was already used differently",
                reason=InvalidRequestReason.CONFLICT,
            )

    def apply(
        self,
        node_ids: Sequence[str] | None,
        package: Mapping[str, object],
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        repair_manifest: Mapping[str, object] | None = None,
        request_intent: Mapping[str, object] | None = None,
    ) -> Job:
        intent = _request_intent(request_intent, node_ids)
        existing = self.get_request(request_id, actor=actor, request_intent=intent)
        if existing is not None:
            return existing
        plan = self.preview(
            node_ids,
            package,
            repair_manifest=repair_manifest,
            request_intent=intent,
        )
        # The latest request leads: a preview digest that no longer matches is
        # advisory.  The freshly computed plan for the same intent is applied.
        del plan_digest
        now = self._clock()
        job = AgentUpgradeAdapter.new_rollout(
            allowed=bool(plan.node_ids),
            request_id=request_id,
            kind="agent-upgrade",
            status_reason=None if plan.node_ids else _summary(plan.skipped),
            result={"skipped": dict(plan.skipped)} if plan.skipped else None,
            actor=actor,
            authority_revision=plan.authority_revision,
            targets=list(plan.node_ids),
            payload_digest=plan.plan_digest,
            payload={
                "sources": plan.sources,
                "node_order": list(plan.node_ids),
                "package": plan.package,
                "request_intent": plan.request_intent,
                **(
                    {"repair_manifest": plan.repair_manifest}
                    if plan.repair_manifest is not None
                    else {}
                ),
            },
            current_attempt=0,
            created_at=now,
            updated_at=now,
        )
        try:
            with self._sessions.begin() as session:
                existing = session.scalar(
                    select(Job).where(Job.request_id == request_id).with_for_update()
                )
                if existing is not None:
                    self._check_replayed_request(existing, actor, intent)
                    session.expunge(existing)
                    return existing
                session.add(job)
                session.flush()
                self._supersede_older(session, job, now)
                if plan.node_ids:
                    self._advance(session, job)
        except IntegrityError:
            existing = self.get_request(request_id, actor=actor, request_intent=intent)
            if existing is not None:
                return existing
            raise
        self._operations.notify_available()
        return job

    def _retire_rollout(self, parent: Job, now: datetime, reason: str) -> None:
        """A stored rollout that cannot be read ends as failed, with a residue.

        Nothing re-derives a damaged plan, and dispatching a Spark from a plan
        that does not verify would install an unproven package, so the rollout
        is ended (the operator starts a new one) instead of waiting or raising.
        """

        retire_as_unknown(
            "agent-upgrade.rollout",
            parent.id,
            BookkeepingReason.PERSISTED_STATE_DAMAGED,
            reason,
        )
        self._rollouts.fail(parent, now, reason)

    def resume(self, job_id: str) -> None:
        """Resume only the durable agent-operation side of an upgrade rollout."""

        now = self._clock()
        with self._sessions.begin() as session:
            parent = session.scalar(
                select(Job).where(Job.id == job_id).with_for_update(of=Job)
            )
            if parent is None:
                raise MissingRecord(job_id, reason=InvalidRequestReason.NOT_FOUND)
            if parent.kind != "agent-upgrade":
                raise InvalidValue(
                    "job is not a resumable agent upgrade",
                    reason=InvalidRequestReason.NOT_READY,
                )
            failed_dispatch = (
                parent.state == "failed"
                and parent.status_reason == UNSUPPORTED_DISPATCH
            )
            stale_dispatch = parent.state == "running"
            if (
                parent.state
                not in job_states.words(
                    LifecycleState.QUEUED, LifecycleState.NEEDS_OPERATOR
                )
                and not failed_dispatch
                and not stale_dispatch
            ):
                raise InvalidValue(
                    "job is not a resumable agent upgrade",
                    reason=InvalidRequestReason.NOT_READY,
                )
            worker_attempt = (
                None
                if parent.current_attempt == 0
                else session.scalar(
                    select(JobAttempt)
                    .where(
                        JobAttempt.job_id == parent.id,
                        JobAttempt.attempt == parent.current_attempt,
                    )
                    .with_for_update(of=JobAttempt)
                )
            )
            if parent.current_attempt > 0 and worker_attempt is None:
                # The dispatch audit row is gone, so no worker can hold a lease
                # on it: it is recorded and read as a lapsed dispatch.
                retire_as_unknown(
                    "agent-upgrade.dispatch-audit",
                    parent.id,
                    BookkeepingReason.ROW_INCOMPLETE,
                    "the worker dispatch audit of the rollout is missing",
                )
            if worker_attempt is not None and worker_attempt.state == "running":
                if _aware(worker_attempt.lease_deadline) > _aware(now):
                    raise InvalidValue(
                        "agent upgrade worker dispatch is still active",
                        reason=InvalidRequestReason.NOT_READY,
                    )
                self._rollouts.expire_worker_attempt(worker_attempt)
            if failed_dispatch or stale_dispatch:
                if failed_dispatch and (
                    worker_attempt is None or worker_attempt.state != "failed"
                ):
                    # The failed dispatch is the evidence that reopening is
                    # safe, and it is not there: reopening only projects the
                    # rollout again (nothing is dispatched until the plan below
                    # checks out), so it is recorded and carried on.
                    retire_as_unknown(
                        "agent-upgrade.dispatch-audit",
                        parent.id,
                        BookkeepingReason.EVIDENCE_MISMATCH,
                        "the failed dispatch of the rollout has no failed audit",
                    )
                if (
                    stale_dispatch
                    and worker_attempt is not None
                    and not job_states.attempt_lapsed(worker_attempt)
                ):
                    raise InvalidValue(
                        "agent upgrade worker dispatch is not stale",
                        reason=InvalidRequestReason.NOT_READY,
                    )
            package = parent.payload.get("package")
            order = parent.payload.get("node_order")
            repair = parent.payload.get("repair_manifest")
            request_intent = parent.payload.get("request_intent")
            expected_payload_keys = {
                "node_order",
                "package",
                "request_intent",
                "sources",
            }
            if repair is not None:
                expected_payload_keys.add("repair_manifest")
            if (
                set(parent.payload) != expected_payload_keys
                or not isinstance(package, dict)
                or not isinstance(order, list)
                or not order
                or not all(isinstance(node_id, str) for node_id in order)
                or len(order) != len(set(order))
                or order != parent.targets
                or (repair is not None and not isinstance(repair, Mapping))
            ):
                self._retire_rollout(
                    parent, now, "stored agent upgrade plan is invalid"
                )
                return
            try:
                normalized_intent = _request_intent(request_intent, None)
            except AgentUpgradeConflict:
                self._retire_rollout(
                    parent, now, "stored agent upgrade plan is invalid"
                )
                return
            try:
                normalized_package = self._package(package)
                normalized_repair = (
                    None
                    if repair is None
                    else self._repair_manifest(repair, normalized_package)
                )
            except AgentUpgradeConflict:
                self._retire_rollout(
                    parent, now, "stored agent upgrade plan is invalid"
                )
                return
            if normalized_repair is not None and order != [
                normalized_repair["node_id"]
            ]:
                self._retire_rollout(
                    parent, now, "stored agent upgrade plan is invalid"
                )
                return
            plan_digest = hashlib.sha256(
                canonical_message(
                    {
                        "sources": parent.payload["sources"],
                        "authority_revision": parent.authority_revision,
                        "node_ids": order,
                        "package": normalized_package,
                        "request_intent": normalized_intent,
                        **(
                            {"repair_manifest": normalized_repair}
                            if normalized_repair is not None
                            else {}
                        ),
                    }
                )
            ).hexdigest()
            if parent.payload_digest != plan_digest:
                self._retire_rollout(
                    parent, now, "stored agent upgrade plan is invalid"
                )
                return
            from vonk_agent_protocol.contracts import AgentUpgradePayload

            sources = parent.payload["sources"]
            if not isinstance(sources, dict) or set(sources) != set(order):
                self._retire_rollout(parent, now, "stored rollback sources are invalid")
                return
            stored_operations = list(
                session.scalars(
                    select(AgentOperation)
                    .where(AgentOperation.parent_job_id == parent.id)
                    .order_by(AgentOperation.created_at, AgentOperation.id)
                    .with_for_update(of=AgentOperation)
                )
            )
            waiting = [
                operation
                for operation in stored_operations
                if operation.state in agent_operation_states.PARKED
            ]
            active = [
                operation
                for operation in stored_operations
                if operation.state in {"queued", "running"}
            ]
            if len({operation.node_id for operation in stored_operations}) != len(
                stored_operations
            ):
                self._retire_rollout(
                    parent, now, "stored agent upgrade operation is invalid"
                )
                return
            for operation in stored_operations:
                payload = read_stored_model(AgentUpgradePayload, operation.payload)
                source = read_stored_model(
                    AgentPackageSource, sources[operation.node_id]
                )
                if (
                    operation.kind != "agent.upgrade.v1"
                    or operation.node_id not in order
                    or operation.authority_revision != parent.authority_revision
                    or {
                        key: value
                        for key, value in operation.payload.items()
                        if key
                        not in {
                            "rollback",
                            "source_package_bytes",
                            "source_package_url",
                        }
                    }
                    != package
                    or payload.rollback.source != source.package
                    or payload.source_package_bytes != source.package_bytes
                    or payload.source_package_url != source.package_url
                    or operation.payload_digest
                    != hashlib.sha256(canonical_message(operation.payload)).hexdigest()
                ):
                    self._retire_rollout(
                        parent, now, "stored agent upgrade operation is invalid"
                    )
                    return
            # Deferred (offline) Sparks are passed over while later ones
            # upgrade, so materialized operations need not form a prefix.
            for operation in active:
                if operation.state == "queued":
                    if operation.current_attempt != 0:
                        self._retire_rollout(
                            parent, now, "stored agent upgrade attempt is invalid"
                        )
                        return
                    continue
                active_attempt = session.scalar(
                    select(AgentOperationAttempt)
                    .where(
                        AgentOperationAttempt.operation_id == operation.id,
                        AgentOperationAttempt.attempt == operation.current_attempt,
                    )
                    .with_for_update(of=AgentOperationAttempt)
                )
                if active_attempt is None or active_attempt.state != "running":
                    self._retire_rollout(
                        parent, now, "stored agent upgrade attempt is invalid"
                    )
                    return
            if failed_dispatch:
                self._rollouts.reopen(parent, now, reason=None)
            else:
                self._rollouts.project(parent, now, reason=None)
            if waiting:
                for operation in waiting:
                    attempt = session.scalar(
                        select(AgentOperationAttempt)
                        .where(
                            AgentOperationAttempt.operation_id == operation.id,
                            AgentOperationAttempt.attempt == operation.current_attempt,
                        )
                        .with_for_update(of=AgentOperationAttempt)
                    )
                    if attempt is None or attempt.state not in {
                        "failed",
                        *agent_operation_states.ATTEMPT_OBSERVING,
                    }:
                        self._retire_rollout(
                            parent, now, "stored agent upgrade attempt is invalid"
                        )
                        return
                    # Operator resume is a new dispatch decision. For an
                    # attempted install it must establish a fresh full safety
                    # fence regardless of the stored helper result. Old agents
                    # can omit or reshape that result, and even a success
                    # acknowledgement is not proof that the new runtime took
                    # over. This prevents the resumed request from overlapping
                    # an orphaned dpkg or maintainer script.
                    schedule_agent_upgrade_retry(operation, attempt, now)
            else:
                self._advance(session, parent)
        self._operations.notify_available()

    def consume_agent_result(
        self,
        session: Session,
        operation: AgentOperation,
        attempt: AgentOperationAttempt,
        message: AgentResult,
    ) -> None:
        if operation.kind != "agent.upgrade.v1":
            return
        parent = session.scalar(
            select(Job).where(Job.id == operation.parent_job_id).with_for_update(of=Job)
        )
        if parent is None or parent.kind != "agent-upgrade":
            return
        package = parent.payload.get("package")
        if not isinstance(package, dict):
            return
        node = session.scalar(
            select(AgentNode)
            .where(AgentNode.node_id == operation.node_id)
            .with_for_update(of=AgentNode)
        )
        if message.state == "succeeded" and (
            node is not None
            and self._contact_proves_target(node, operation, package, message)
        ):
            self._advance(session, parent)
            return
        if message.state not in {
            "succeeded",
            "failed",
            agent_operation_states.WIRE_UNKNOWN,
        }:
            return
        # A helper acknowledgement without exact fresh identity, and every
        # failure, is retried automatically behind the dpkg safety fence.  A
        # later authenticated contact that proves the target completes the
        # order first; the retry is dispatched only while the Spark still runs
        # the exact rollback source.  Meanwhile the rollout moves on.
        if node is None or node.state != "active" or node.revoked_at is not None:
            AgentOperationAdapter(session).settle(
                operation,
                attempt,
                parent,
                Reported(
                    Outcome.FAILED,
                    retryable=False,
                    reason="Spark is no longer an active enrolled node",
                ),
                self._clock(),
            )
        else:
            schedule_agent_upgrade_retry(operation, attempt, self._clock())
            detail = _failure_detail(attempt.result)
            if detail is not None:
                operation.status_reason = (f"{detail}; {operation.status_reason}")[:512]
        self._advance(session, parent)

    def heal_rollouts(self, limit: int = 8) -> bool:
        """Heal legacy rollouts no order change will reach (a periodic tick).

        A rollout that waits for an operator (nothing advertises an action for it),
        one the generic worker failed as an unsupported kind, and a ``running``
        worker dispatch whose lease lapsed are projected from their orders again.
        A dispatch that still holds a live lease is left alone.  Bounded and
        idempotent: a row another transaction changed meanwhile is left to the
        next pass.  Returns whether anything changed.
        """

        now = self._clock()
        with self._sessions() as session:
            candidates = list(
                session.scalars(
                    select(Job.id)
                    .where(
                        Job.kind == "agent-upgrade",
                        or_(
                            Job.state.in_(
                                job_states.words(
                                    LifecycleState.NEEDS_OPERATOR,
                                    LifecycleState.RUNNING,
                                )
                            ),
                            and_(
                                Job.state == "failed",
                                Job.status_reason == UNSUPPORTED_DISPATCH,
                            ),
                        ),
                    )
                    .order_by(Job.updated_at, Job.id)
                    .limit(limit)
                )
            )
        healed = False
        for job_id in candidates:
            with self._sessions.begin() as session:
                parent = session.scalar(
                    select(Job).where(Job.id == job_id).with_for_update(of=Job)
                )
                if parent is None or parent.kind != "agent-upgrade":
                    continue
                if parent.state == "failed":
                    if parent.status_reason != UNSUPPORTED_DISPATCH:
                        continue
                    self._rollouts.reopen(parent, now, reason=None)
                elif (
                    parent.state not in _ACTIVE_ROLLOUT_STATES
                    or parent.state == "running"
                    and self._dispatch_is_live(session, parent, now)
                ):
                    continue
                self._advance(session, parent)
                healed = True
        if healed:
            self._operations.notify_available()
        return healed

    @staticmethod
    def _dispatch_is_live(session: Session, parent: Job, now: datetime) -> bool:
        attempt = (
            None
            if parent.current_attempt == 0
            else session.scalar(
                select(JobAttempt).where(
                    JobAttempt.job_id == parent.id,
                    JobAttempt.attempt == parent.current_attempt,
                )
            )
        )
        return (
            attempt is not None
            and attempt.state == "running"
            and _aware(attempt.lease_deadline) > _aware(now)
        )

    def advance_node(self, node_id: str) -> None:
        """Resume every rollout that includes ``node_id`` when that Spark polls."""

        now = self._clock()
        with self._sessions.begin() as session:
            job_ids = [
                job_id
                for job_id, targets in session.execute(
                    select(Job.id, Job.targets).where(
                        Job.kind == "agent-upgrade",
                        Job.state.in_(_ACTIVE_ROLLOUT_STATES),
                    )
                )
                if isinstance(targets, list) and node_id in targets
            ]
            for job_id in job_ids:
                parent = session.scalar(
                    select(Job).where(Job.id == job_id).with_for_update(of=Job)
                )
                if parent is None or parent.state not in _ACTIVE_ROLLOUT_STATES:
                    continue
                self._settle_at_target(session, parent, node_id, now)
                self._retry_parked(session, parent, now)
                self._advance(session, parent)

    def _settle_at_target(
        self, session: Session, parent: Job, node_id: str, now: datetime
    ) -> None:
        """Resolve an attempted upgrade whose Spark reports the target build.

        Authenticated contact is the authority on what runs.  An install that
        was attempted and then reported a failure, or that expired, is
        converged by that identity alone; it never waits for a retry, a
        rollback receipt, or an operator.
        """

        package = parent.payload.get("package")
        if not isinstance(package, dict):
            return
        node = session.get(AgentNode, node_id)
        if node is None or not self._at_target(node, package):
            return
        for operation in session.scalars(
            select(AgentOperation)
            .where(
                AgentOperation.parent_job_id == parent.id,
                AgentOperation.node_id == node_id,
                AgentOperation.state.in_(agent_operation_states.PARKED),
                AgentOperation.current_attempt >= 1,
            )
            .with_for_update(of=AgentOperation)
        ):
            AgentOperationAdapter(session).settle(
                operation,
                None,
                parent,
                Reported(
                    Outcome.DONE,
                    reason="Spark reports it already runs the requested agent build",
                ),
                now,
            )

    def _retry_parked(self, session: Session, parent: Job, now: datetime) -> None:
        """Turn a parked order into an automatic, fenced retry."""

        for operation in session.scalars(
            select(AgentOperation)
            .where(
                AgentOperation.parent_job_id == parent.id,
                AgentOperation.state.in_(agent_operation_states.PARKED),
                AgentOperation.next_action_at.is_(None),
            )
            .with_for_update(of=AgentOperation)
        ):
            node = session.get(AgentNode, operation.node_id)
            if node is None or node.state != "active" or node.revoked_at is not None:
                AgentOperationAdapter(session).settle(
                    operation,
                    None,
                    parent,
                    Reported(
                        Outcome.FAILED,
                        retryable=False,
                        reason="Spark is no longer an active enrolled node",
                    ),
                    now,
                )
                continue
            attempt = session.scalar(
                select(AgentOperationAttempt)
                .where(
                    AgentOperationAttempt.operation_id == operation.id,
                    AgentOperationAttempt.attempt == operation.current_attempt,
                )
                .with_for_update(of=AgentOperationAttempt)
            )
            schedule_agent_upgrade_retry(operation, attempt, now)

    def _supersede_older(self, session: Session, job: Job, now: datetime) -> None:
        """The latest fleet upgrade request leads; older rollouts stop advancing.

        An older order already running on a Spark finishes under its own fence;
        queued and retry-parked orders are withdrawn so they cannot compete with
        the new request for the same Spark.
        """

        for older in session.scalars(
            select(Job)
            .where(
                Job.kind == "agent-upgrade",
                Job.state.in_(_ACTIVE_ROLLOUT_STATES),
                Job.id != job.id,
            )
            .with_for_update(of=Job)
        ):
            result = dict(older.result) if isinstance(older.result, Mapping) else {}
            result["superseded_by"] = job.id
            older.result = result
            for operation in session.scalars(
                select(AgentOperation)
                .where(
                    AgentOperation.parent_job_id == older.id,
                    AgentOperation.state.in_(agent_operation_states.QUEUED_OR_PARKED),
                )
                .with_for_update(of=AgentOperation)
            ):
                AgentOperationAdapter(session).settle(
                    operation,
                    None,
                    older,
                    CancelRequested(reason=f"superseded by agent upgrade {job.id}"),
                    now,
                )
            self._advance(session, older)

    def _advance(self, session: Session, parent: Job) -> None:
        """Own the rollout projection: dispatch, defer, or conclude.

        One Spark is upgraded at a time.  A Spark that is offline is deferred
        and the next one proceeds; the deferred Spark is dispatched when it
        polls again.  A Spark that already runs the target, or that can never
        take this package, is skipped.  Failed orders retry automatically, so
        the rollout concludes only when every target is settled.
        """

        if parent.state not in _ACTIVE_ROLLOUT_STATES:
            return
        now = self._clock()
        operations = list(
            session.scalars(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == parent.id)
                .order_by(AgentOperation.created_at, AgentOperation.id)
            )
        )
        if any(
            operation.state == "queued"
            or agent_upgrade_in_flight(session, operation, now)
            for operation in operations
        ):
            if parent.state in job_states.words(LifecycleState.NEEDS_OPERATOR):
                # A legacy wait: no action exists for a rollout, and its orders
                # are moving by themselves.
                self._rollouts.project(parent, now)
            return
        result = dict(parent.result) if isinstance(parent.result, Mapping) else {}
        superseded = result.get("superseded_by")
        if superseded is not None:
            self._rollouts.cancelled(
                parent, now, f"superseded by agent upgrade {superseded}"
            )
            return
        stored_skipped = result.get("skipped")
        skipped: dict[str, str] = {
            str(node_id): str(reason)
            for node_id, reason in (
                stored_skipped.items() if isinstance(stored_skipped, Mapping) else ()
            )
        }
        deferred: list[str] = []
        materialized = {operation.node_id for operation in operations}
        order = parent.payload.get("node_order")
        package = parent.payload.get("package")
        if not isinstance(order, list) or not isinstance(package, dict):
            self._rollouts.fail(parent, now, "stored agent upgrade plan is invalid")
            return
        for node_id in order:
            if not isinstance(node_id, str) or node_id in materialized:
                continue
            if node_id in skipped:
                continue
            node = session.scalar(
                select(AgentNode)
                .where(AgentNode.node_id == node_id)
                .with_for_update(of=AgentNode)
            )
            if node is None:
                skipped[node_id] = "does not exist"
                continue
            if self._at_target(node, package):
                skipped[node_id] = _ALREADY_CURRENT
                continue
            reason = self._permanent_ineligible_reason(node, package)
            if reason is not None:
                skipped[node_id] = reason
                continue
            if self._ineligible_reason(node, package, now) is not None:
                deferred.append(node_id)
                continue
            not_queued = self._enqueue_node(session, parent, node_id)
            if not_queued is not None:
                skipped[node_id] = not_queued
                continue
            self._record(parent, result, skipped)
            self._rollouts.project(parent, now, reason=None)
            return
        self._record(parent, result, skipped)
        retrying = [
            operation
            for operation in operations
            # A dispatched order whose Spark went dark past its fence no
            # longer holds the fleet, but it is not settled either.
            if operation.state in agent_operation_states.RUNNING_OR_PARKED
        ]
        if deferred or retrying:
            self._rollouts.project(
                parent,
                now,
                reason=(
                    f"Spark {deferred[0]} is not currently online; its upgrade "
                    "resumes automatically when it reconnects"
                    if deferred
                    else retrying[0].status_reason
                    or f"Spark {retrying[0].node_id} upgrade retries automatically"
                ),
            )
            return
        failed = [operation for operation in operations if operation.state == "failed"]
        if failed:
            self._rollouts.fail(
                parent,
                now,
                f"Spark {failed[0].node_id} upgrade failed: "
                f"{failed[0].status_reason or 'see operation evidence'}",
            )
        else:
            self._rollouts.succeed(parent, now, reason=_summary(skipped))

    @staticmethod
    def _record(
        parent: Job, result: dict[str, object], skipped: dict[str, str]
    ) -> None:
        if skipped != (result.get("skipped") or {}):
            parent.result = {**result, "skipped": skipped}

    @classmethod
    def _contact_proves_target(
        cls,
        node: AgentNode,
        operation: AgentOperation,
        package: Mapping[str, object],
        message: AgentResult,
    ) -> bool:
        last_seen = node.last_seen_at
        if last_seen is None:
            return False
        observed = (
            last_seen if last_seen.tzinfo is not None else last_seen.replace(tzinfo=UTC)
        )
        dispatched = (
            operation.created_at
            if operation.created_at.tzinfo is not None
            else operation.created_at.replace(tzinfo=UTC)
        )
        evidence = message.result
        from vonk_agent_protocol.contracts import AgentUpgradePayload
        from vonk_agent_protocol.package_upgrade import PackageActivationReceipt

        from .package_activation import matches_receipt

        raw_receipt = evidence.get("activation_receipt")
        if raw_receipt is None:
            return False
        receipt = read_stored_model(PackageActivationReceipt, raw_receipt)
        if receipt.phase != "acknowledged" or not matches_receipt(
            receipt,
            read_stored_model(AgentUpgradePayload, operation.payload),
            node.node_id,
        ):
            return False
        return bool(
            observed >= dispatched
            and node.state == "active"
            and node.revoked_at is None
            and node.protocol_version == AGENT_PROTOCOL_VERSION
            and node.architecture == package.get("architecture")
            # Signed package and binary/build digests are the compatibility
            # identity.  Version strings remain audit metadata and may differ
            # across packaging schemes without invalidating an exact upgrade.
            and node.build_digest == package.get("target_build_digest")
            and node.binary_digest == package.get("target_binary_digest")
            and evidence.get("architecture") == package.get("architecture")
            and evidence.get("build_digest") == package.get("target_build_digest")
            and evidence.get("binary_digest") == package.get("target_binary_digest")
            and evidence.get("package_sha256") == package.get("package_sha256")
            and evidence.get("status") == "upgraded"
        )

    def _enqueue_node(self, session: Session, parent: Job, node_id: str) -> str | None:
        """Queue the upgrade order for one Spark.

        Returns why this Spark is skipped instead (a stored package or rollback
        source that cannot be used, a Spark whose installed agent no longer matches
        its rollback source), else ``None`` once the order is queued.  The rollout
        records the reason and goes on to the next Spark; nothing is raised.
        """

        package = parent.payload.get("package")
        if not isinstance(package, dict):
            return "stored agent upgrade package is invalid"
        stored_sources = parent.payload.get("sources")
        stored_source = (
            stored_sources.get(node_id) if isinstance(stored_sources, Mapping) else None
        )
        if stored_source is None:
            return "stored rollback sources are invalid"
        try:
            source = read_stored_model(AgentPackageSource, stored_source)
        except (TypeError, ValueError):
            return "stored rollback sources are invalid"
        node = session.get(AgentNode, node_id)
        if (
            node is None
            or node.binary_digest != source.package.binary_sha256
            or node.build_digest != source.build_digest
        ):
            return "rollback source no longer matches installed agent"
        payload = {
            **package,
            "source_package_bytes": source.package_bytes,
            "source_package_url": source.package_url,
            "rollback": {
                "source": source.package.model_dump(mode="json"),
                "attempt_nonce": secrets.token_hex(32),
                "activation_deadline": int(self._clock().timestamp()) + 900,
            },
        }
        self._operations.enqueue_in_session(
            session,
            parent.id,
            node_id,
            "agent.upgrade.v1",
            parent.authority_revision,
            payload,
            operation_id=str(uuid.uuid4()),
        )
        return None

    @staticmethod
    def _package(value: Mapping[str, object]) -> dict[str, object]:
        document = dict(value)
        required = {
            "architecture",
            "package_bytes",
            "package_sha256",
            "package_signature",
            "package_url",
            "package_version",
            "schema_version",
            "target_binary_digest",
            "target_build_digest",
        }
        url = document.get("package_url")
        if (
            set(document) != required
            or document.get("schema_version") != 1
            or document.get("architecture") != "linux-arm64"
            or not isinstance(document.get("package_bytes"), int)
            or isinstance(document.get("package_bytes"), bool)
            or not 1
            <= require_integer(document["package_bytes"], "package bytes")
            <= 1024**3
            or not isinstance(document.get("package_sha256"), str)
            or _SHA256.fullmatch(str(document["package_sha256"])) is None
            or not isinstance(document.get("package_signature"), str)
            or _SIGNATURE.fullmatch(str(document["package_signature"])) is None
            or not isinstance(document.get("package_version"), str)
            or _PACKAGE_VERSION.fullmatch(str(document["package_version"])) is None
            or not isinstance(document.get("target_binary_digest"), str)
            or _SHA256.fullmatch(str(document["target_binary_digest"])) is None
            or not isinstance(document.get("target_build_digest"), str)
            or _BUILD_DIGEST.fullmatch(str(document["target_build_digest"])) is None
            or not isinstance(url, str)
            or not url.startswith("https://install.vonkforge.ai/")
            or not url.endswith("/vonk-forge-agent.deb")
            or any(marker in url for marker in ("?", "#", "@"))
        ):
            raise AgentUpgradeInvalid(
                "agent upgrade package is invalid",
                reason=InvalidRequestReason.MALFORMED,
            )
        return document

    @classmethod
    def _repair_manifest(
        cls,
        value: Mapping[str, object],
        package: Mapping[str, object],
    ) -> dict[str, object]:
        document = dict(value)
        manifest_package_value = document.get("package")
        if (
            set(document)
            != {
                "authority_sha256",
                "kind",
                "node_id",
                "package",
                "schema_version",
            }
            or document.get("schema_version") != 2
            or document.get("kind") != "agent-upgrade-repair"
            or not isinstance(document.get("node_id"), str)
            or _NODE_ID.fullmatch(str(document["node_id"])) is None
            or not isinstance(document.get("authority_sha256"), str)
            or _SHA256.fullmatch(str(document["authority_sha256"])) is None
            or not isinstance(manifest_package_value, Mapping)
        ):
            raise AgentUpgradeInvalid(
                "agent repair manifest is invalid",
                reason=InvalidRequestReason.MALFORMED,
            )
        manifest_package = cls._package(manifest_package_value)
        expected_url = (
            "https://install.vonkforge.ai/repair-capsules/"
            f"{document['node_id']}/{document['authority_sha256']}/"
            f"{manifest_package['package_sha256']}/vonk-forge-agent.deb"
        )
        if manifest_package["package_url"] != expected_url:
            raise AgentUpgradeInvalid(
                "agent repair package URL is not canonical",
                reason=InvalidRequestReason.MALFORMED,
            )
        if dict(package) != manifest_package:
            raise AgentUpgradeInvalid(
                "agent repair manifest does not match its package descriptor",
                reason=InvalidRequestReason.CONFLICT,
            )
        return {**document, "package": manifest_package}

    @staticmethod
    def _permanent_ineligible_reason(
        node: AgentNode, package: Mapping[str, object]
    ) -> str | None:
        """A reason this package can never be dispatched to ``node``."""

        if node.state != "active" or node.revoked_at is not None:
            return "is not active"
        if node.architecture != package["architecture"]:
            return "has an incompatible architecture"
        return None

    @classmethod
    def _ineligible_reason(
        cls,
        node: AgentNode,
        package: Mapping[str, object],
        now: datetime,
    ) -> str | None:
        reason = cls._permanent_ineligible_reason(node, package)
        if reason is not None:
            return reason
        current = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        last_seen = node.last_seen_at
        if last_seen is None:
            return "has never reported online"
        seen = (
            last_seen if last_seen.tzinfo is not None else last_seen.replace(tzinfo=UTC)
        )
        if seen > current or current - seen > _ONLINE_WINDOW:
            return "is not currently online"
        return None

    @staticmethod
    def _at_target(node: AgentNode, package: Mapping[str, object]) -> bool:
        return bool(
            node.build_digest == package["target_build_digest"]
            and node.binary_digest == package["target_binary_digest"]
        )
