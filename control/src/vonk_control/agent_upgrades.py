"""Controller-owned rolling upgrades for enrolled Spark agents."""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import AgentResult, canonical_message

from .agent_jobs import AgentJobService
from .models import AgentNode, AgentOperation, AgentOperationAttempt, Job, JobAttempt

_PACKAGE_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+~-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_BUILD_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SIGNATURE = re.compile(r"[0-9a-f]{128}\Z")
_ONLINE_WINDOW = timedelta(seconds=150)
# packaging/debian/preinst gives the asynchronous bridge unit 180 seconds to
# outlive the failed dpkg invocation and restart the helper.  A helper stop can
# then consume its 15-second TimeoutStopSec.  Keep another 45 seconds for PID 1
# dispatch, polling, and controller/agent scheduling jitter.  This durable gate
# is deliberately coupled to those package-side budgets: the single automatic
# retry must not enter the old helper namespace while its bridge is still live.
_HELPER_BRIDGE_RUNTIME_MAX = timedelta(seconds=180)
_HELPER_STOP_TIMEOUT = timedelta(seconds=15)
_HELPER_BRIDGE_DISPATCH_MARGIN = timedelta(seconds=45)
_HELPER_BRIDGE_RECOVERY_BACKOFF = (
    _HELPER_BRIDGE_RUNTIME_MAX
    + _HELPER_STOP_TIMEOUT
    + _HELPER_BRIDGE_DISPATCH_MARGIN
)
_TARGET_PROTOCOL_VERSION = 3
_RECOVERABLE_HELPER_BRIDGE_FAILURES = frozenset(
    {
        "agent upgrade request is invalid",
        "agent upgrade helper is unavailable",
        "agent upgrade helper rejected the request",
        "agent upgrade helper rejected the request: operation_failed",
        "agent upgrade did not restart the service",
    }
)
_RETRYABLE_HELPER_BRIDGE_FAILURES = _RECOVERABLE_HELPER_BRIDGE_FAILURES - {
    "agent upgrade did not restart the service"
}


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class AgentUpgradeConflict(RuntimeError):
    """An agent upgrade plan is invalid, stale, or not safely executable."""


@dataclass(frozen=True, slots=True)
class AgentUpgradePlan:
    authority_revision: str
    node_ids: tuple[str, ...]
    package: dict[str, object]
    plan_digest: str
    strategy: str


class AgentUpgradeService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        operations: AgentJobService,
        *,
        clock: Callable[[], datetime],
        current_revision: Callable[[], str],
        channel: str = "dev",
        release_api_url: str = "https://install.vonkforge.ai",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._sessions = sessions
        self._operations = operations
        self._clock = clock
        self._current_revision = current_revision
        if channel not in {"dev", "stable"}:
            raise ValueError("agent upgrade channel is invalid")
        self._channel = channel
        self._http = httpx.Client(
            base_url=release_api_url,
            follow_redirects=False,
            timeout=httpx.Timeout(15.0, connect=5.0),
            trust_env=False,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def current_package(self) -> dict[str, object]:
        prefix = f"/artifacts/{self._channel}"
        try:
            manifest_response = self._http.get(f"{prefix}/current.manifest")
            manifest_response.raise_for_status()
            if len(manifest_response.content) > 64 * 1024:
                raise AgentUpgradeConflict("agent release manifest is too large")
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
                raise AgentUpgradeConflict("agent release manifest is invalid")
            release_response = self._http.get(f"/{release_path}")
            release_response.raise_for_status()
            if len(release_response.content) > 256 * 1024:
                raise AgentUpgradeConflict("agent release document is too large")
            release = release_response.json()
            artifact = release["artifacts"]["agent-package-linux-arm64"]
            signature_record = release["artifacts"][
                "agent-package-signature-linux-arm64"
            ]
            signature_response = self._http.get(f"/{signature_record['path']}")
            signature_response.raise_for_status()
            signature = signature_response.text.strip()
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
            raise AgentUpgradeConflict(
                "current agent release is unavailable"
            ) from error
        if (
            release.get("channel") != self._channel
            or release.get("generation") != generation
            or artifact.get("host_signature") != signature
            or signature_record.get("sha256")
            != hashlib.sha256(signature_response.content).hexdigest()
            or signature_record.get("size") != len(signature_response.content)
        ):
            raise AgentUpgradeConflict("current agent release is inconsistent")
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
        strategy: str = "one-at-a-time",
    ) -> AgentUpgradePlan:
        if strategy not in {"one-at-a-time", "all-at-once"}:
            raise AgentUpgradeConflict("agent upgrade rollout strategy is invalid")
        payload = self._package(package)
        authority_revision = self._current_revision()
        now = self._clock()
        with self._sessions() as session:
            requested = None if node_ids is None else tuple(node_ids)
            if requested is not None and (
                not requested
                or len(requested) != len(set(requested))
                or len(requested) > 64
            ):
                raise AgentUpgradeConflict("agent upgrade targets are invalid")
            candidates = list(
                session.scalars(
                    select(AgentNode)
                    if requested is None
                    else select(AgentNode).where(AgentNode.node_id.in_(requested))
                )
            )
            nodes = {node.node_id: node for node in candidates}
            targets = (
                tuple(
                    sorted(
                        node.node_id
                        for node in candidates
                        if self._eligible(node, payload, now)
                        and not self._at_target(node, payload)
                    )
                )
                if requested is None
                else requested
            )
            if not targets:
                raise AgentUpgradeConflict(
                    "no outdated upgrade-capable Sparks were found"
                )
            if requested is not None and set(nodes) != set(targets):
                raise AgentUpgradeConflict("agent upgrade target does not exist")
            for node_id in targets:
                node = nodes[node_id]
                reason = self._ineligible_reason(node, payload, now)
                if reason is not None:
                    raise AgentUpgradeConflict(f"Spark {node_id} {reason}")
                if self._at_target(node, payload):
                    raise AgentUpgradeConflict(
                        f"Spark {node_id} already runs the requested agent build"
                    )
        document = {
            "authority_revision": authority_revision,
            "node_ids": list(targets),
            "package": payload,
            "strategy": strategy,
        }
        return AgentUpgradePlan(
            authority_revision=authority_revision,
            node_ids=targets,
            package=payload,
            plan_digest=hashlib.sha256(canonical_message(document)).hexdigest(),
            strategy=strategy,
        )

    def apply(
        self,
        node_ids: Sequence[str] | None,
        package: Mapping[str, object],
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        strategy: str = "one-at-a-time",
    ) -> Job:
        plan = self.preview(node_ids, package, strategy=strategy)
        if plan.plan_digest != plan_digest:
            raise AgentUpgradeConflict("agent upgrade preview is stale")
        now = self._clock()
        job = Job(
            request_id=request_id,
            kind="agent-upgrade",
            state="queued",
            actor=actor,
            authority_revision=plan.authority_revision,
            targets=list(plan.node_ids),
            payload_digest=plan.plan_digest,
            payload={
                "node_order": list(plan.node_ids),
                "package": plan.package,
                "strategy": plan.strategy,
            },
            current_attempt=0,
            created_at=now,
            updated_at=now,
        )
        with self._sessions.begin() as session:
            session.add(job)
            session.flush()
            if plan.strategy == "all-at-once":
                for node_id in plan.node_ids:
                    self._enqueue_node(session, job, node_id)
            else:
                self._enqueue_next(session, job)
        self._operations.notify_available()
        return job

    def resume(self, job_id: str) -> None:
        """Resume only the durable agent-operation side of an upgrade rollout."""

        now = self._clock()
        with self._sessions.begin() as session:
            parent = session.scalar(
                select(Job).where(Job.id == job_id).with_for_update(of=Job)
            )
            if parent is None:
                raise KeyError(job_id)
            if parent.kind != "agent-upgrade":
                raise ValueError("job is not a resumable agent upgrade")
            failed_dispatch = parent.state == "failed" and parent.status_reason == (
                "unsupported job kind: agent-upgrade"
            )
            stale_dispatch = parent.state == "running"
            if (
                parent.state != "waiting-for-operator"
                and not failed_dispatch
                and not stale_dispatch
            ):
                raise ValueError("job is not a resumable agent upgrade")
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
                raise ValueError("agent upgrade worker dispatch audit is invalid")
            if worker_attempt is not None and worker_attempt.state == "running":
                if _aware(worker_attempt.lease_deadline) > _aware(now):
                    raise ValueError("agent upgrade worker dispatch is still active")
                worker_attempt.state = "expired"
            if failed_dispatch or stale_dispatch:
                if failed_dispatch and (
                    worker_attempt is None or worker_attempt.state != "failed"
                ):
                    raise ValueError("failed agent upgrade dispatch audit is invalid")
                if stale_dispatch and (
                    worker_attempt is None or worker_attempt.state != "expired"
                ):
                    raise ValueError("agent upgrade worker dispatch is not stale")
            package = parent.payload.get("package")
            order = parent.payload.get("node_order")
            strategy = parent.payload.get("strategy")
            if (
                set(parent.payload) != {"node_order", "package", "strategy"}
                or not isinstance(package, dict)
                or not isinstance(order, list)
                or not order
                or not all(isinstance(node_id, str) for node_id in order)
                or len(order) != len(set(order))
                or order != parent.targets
                or strategy not in {"one-at-a-time", "all-at-once"}
            ):
                raise ValueError("stored agent upgrade plan is invalid")
            try:
                normalized_package = self._package(package)
            except AgentUpgradeConflict as error:
                raise ValueError("stored agent upgrade plan is invalid") from error
            plan_digest = hashlib.sha256(
                canonical_message(
                    {
                        "authority_revision": parent.authority_revision,
                        "node_ids": order,
                        "package": normalized_package,
                        "strategy": strategy,
                    }
                )
            ).hexdigest()
            if parent.payload_digest != plan_digest:
                raise ValueError("stored agent upgrade plan is invalid")
            payload_digest = hashlib.sha256(canonical_message(package)).hexdigest()
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
                if operation.state == "waiting-for-operator"
            ]
            active = [
                operation
                for operation in stored_operations
                if operation.state in {"queued", "running"}
            ]
            if len({operation.node_id for operation in stored_operations}) != len(
                stored_operations
            ):
                raise ValueError("stored agent upgrade operation is invalid")
            for operation in stored_operations:
                if (
                    operation.kind != "agent.upgrade.v1"
                    or operation.node_id not in order
                    or operation.authority_revision != parent.authority_revision
                    or operation.payload != package
                    or operation.payload_digest != payload_digest
                ):
                    raise ValueError("stored agent upgrade operation is invalid")
            materialized = {
                operation.node_id: operation for operation in stored_operations
            }
            if strategy == "all-at-once":
                if set(materialized) != set(order):
                    raise ValueError("stored agent upgrade topology is invalid")
            else:
                expected_prefix = order[: len(materialized)]
                if not materialized or set(materialized) != set(expected_prefix):
                    raise ValueError("stored agent upgrade topology is invalid")
                if any(
                    materialized[node_id].state != "succeeded"
                    for node_id in expected_prefix[:-1]
                ):
                    raise ValueError("stored agent upgrade topology is invalid")
                if materialized[expected_prefix[-1]].state not in {
                    "queued",
                    "running",
                    "succeeded",
                    "waiting-for-operator",
                }:
                    raise ValueError("stored agent upgrade topology is invalid")
            for operation in active:
                if operation.state == "queued":
                    if operation.current_attempt != 0:
                        raise ValueError("stored agent upgrade attempt is invalid")
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
                    raise ValueError("stored agent upgrade attempt is invalid")
            parent.state = "queued"
            parent.status_reason = None
            parent.updated_at = now
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
                        "expired",
                        "failed",
                        "waiting-for-operator",
                    }:
                        raise ValueError("stored agent upgrade attempt is invalid")
                    operation.retry_disposition = "retry"
                    operation.retry_disposition_attempt = operation.current_attempt
                    operation.updated_at = now
                    result = attempt.result
                    reason = (
                        result.get("reason") if isinstance(result, Mapping) else None
                    )
                    # Operator resume is a new dispatch decision. For an
                    # ambiguous helper failure it must establish a fresh full
                    # safety fence, even if an older attempt deadline already
                    # elapsed. This prevents the resumed request from
                    # overlapping an orphaned dpkg or maintainer script.
                    not_before = (
                        now + _HELPER_BRIDGE_RECOVERY_BACKOFF
                        if reason in _RECOVERABLE_HELPER_BRIDGE_FAILURES
                        else now
                    )
                    attempt.lease_deadline = max(
                        _aware(attempt.lease_deadline), _aware(not_before)
                    )
            elif not active:
                if len(stored_operations) == len(order) and all(
                    operation.state == "succeeded" for operation in stored_operations
                ):
                    parent.state = "succeeded"
                    return
                before = len(stored_operations)
                self._enqueue_next(session, parent)
                if parent.state != "queued":
                    raise ValueError(
                        "next agent upgrade target is not currently eligible"
                    )
                session.flush()
                after = int(
                    session.scalar(
                        select(func.count())
                        .select_from(AgentOperation)
                        .where(AgentOperation.parent_job_id == parent.id)
                    )
                    or 0
                )
                if after != before + 1:
                    raise ValueError("agent upgrade has no operation to resume")
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
        if message.state == "succeeded":
            if node is None or not self._contact_proves_target(
                node, operation, package, message
            ):
                # A helper acknowledgement or generic health report is not proof
                # that the newly installed binary restarted successfully.  Keep
                # the operation reconcilable until a subsequent authenticated
                # protocol-v3 contact reports the exact published identity.
                self._wait_for_identity(operation, attempt, now=self._clock())
                return
            if parent.state == "waiting-for-operator":
                parent.state = "queued"
                parent.status_reason = None
                parent.updated_at = self._clock()
        elif (bridge_failure := self._helper_bridge_failure(message)) is not None:
            # The original signed-helper bridge could reject the first request
            # before dpkg changed the host.  Permit one subsequent agent poll to
            # retry that exact request, then leave it waiting for identity-only
            # reconciliation instead of forming an unbounded retry loop.
            retry = bool(
                attempt.attempt == 1
                and operation.current_attempt == 1
                and bridge_failure in _RETRYABLE_HELPER_BRIDGE_FAILURES
                and node is not None
                and self._safe_to_retry(node, package)
            )
            self._wait_for_identity(
                operation,
                attempt,
                now=self._clock(),
                retry=retry,
                preserve_failed_attempt=True,
            )
            return
        else:
            return
        if parent.payload.get("strategy") == "one-at-a-time":
            self._enqueue_next(session, parent)

    @staticmethod
    def _wait_for_identity(
        operation: AgentOperation,
        attempt: AgentOperationAttempt,
        *,
        now: datetime,
        retry: bool = False,
        preserve_failed_attempt: bool = False,
    ) -> None:
        operation.state = "waiting-for-operator"
        operation.updated_at = now
        if not preserve_failed_attempt:
            attempt.state = "waiting-for-operator"
        operation.retry_disposition = "retry" if retry else None
        operation.retry_disposition_attempt = attempt.attempt if retry else None
        if preserve_failed_attempt:
            # The package preinst asks PID 1 to restart the old sandboxed helper
            # only after the failed dpkg process exits. Every ambiguous helper
            # failure persists the full bridge runtime, helper stop timeout, and
            # dispatch margin. The automatic retry and any later operator resume
            # must both respect this durable not-before fence.
            # AgentOperationAttempt is already the durable owner of attempt
            # timing, including after controller restarts.
            attempt.lease_deadline = now + _HELPER_BRIDGE_RECOVERY_BACKOFF

    @staticmethod
    def _helper_bridge_failure(message: AgentResult) -> str | None:
        reason = message.result.get("reason")
        return (
            reason
            if (
                message.state == "failed"
                and isinstance(reason, str)
                and reason in _RECOVERABLE_HELPER_BRIDGE_FAILURES
            )
            else None
        )

    @classmethod
    def _contact_proves_target(
        cls,
        node: AgentNode,
        operation: AgentOperation,
        package: Mapping[str, object],
        message: AgentResult,
    ) -> bool:
        semantic_version = cls._target_semantic_version(package)
        last_seen = node.last_seen_at
        if semantic_version is None or last_seen is None:
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
        return bool(
            observed >= dispatched
            and node.state == "active"
            and node.revoked_at is None
            and node.protocol_version == _TARGET_PROTOCOL_VERSION
            and "agent.runtime.rust.v1" in set(node.capabilities or ())
            and "agent.upgrade.v1" in set(node.capabilities or ())
            and node.architecture == package.get("architecture")
            and node.semantic_version == semantic_version
            and node.build_digest == package.get("target_build_digest")
            and node.binary_digest == package.get("target_binary_digest")
            and node.self_test_passed is True
            and evidence.get("architecture") == package.get("architecture")
            and evidence.get("build_digest") == package.get("target_build_digest")
            and evidence.get("binary_digest") == package.get("target_binary_digest")
            and evidence.get("package_sha256") == package.get("package_sha256")
            and evidence.get("package_version") == package.get("package_version")
            and evidence.get("self_test_passed") is True
            and evidence.get("status") == "upgraded"
        )

    @staticmethod
    def _target_semantic_version(package: Mapping[str, object]) -> str | None:
        version = package.get("package_version")
        if not isinstance(version, str):
            return None
        match = re.match(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version)
        return None if match is None else match.group(0)

    @staticmethod
    def _safe_to_retry(
        node: AgentNode,
        package: Mapping[str, object],
    ) -> bool:
        return bool(
            node.state == "active"
            and node.revoked_at is None
            and node.protocol_version == _TARGET_PROTOCOL_VERSION
            and "agent.upgrade.v1" in set(node.capabilities or ())
            and node.architecture == package.get("architecture")
            and node.self_test_passed is True
            and (
                node.build_digest != package.get("target_build_digest")
                or node.binary_digest != package.get("target_binary_digest")
            )
        )

    def _enqueue_next(self, session: Session, parent: Job) -> None:
        package = parent.payload.get("package")
        order = parent.payload.get("node_order")
        if not isinstance(package, dict) or not isinstance(order, list):
            raise AgentUpgradeConflict("stored agent upgrade plan is invalid")
        existing = set(
            session.scalars(
                select(AgentOperation.node_id).where(
                    AgentOperation.parent_job_id == parent.id
                )
            )
        )
        next_node = next(
            (
                node_id
                for node_id in order
                if isinstance(node_id, str) and node_id not in existing
            ),
            None,
        )
        if next_node is None:
            return
        node = session.scalar(
            select(AgentNode)
            .where(AgentNode.node_id == next_node)
            .with_for_update(of=AgentNode)
        )
        reason = (
            "does not exist"
            if node is None
            else self._ineligible_reason(node, package, self._clock())
        )
        if reason is not None:
            parent.state = "waiting-for-operator"
            parent.status_reason = f"Spark {next_node} {reason}"
            parent.updated_at = self._clock()
            return
        self._enqueue_node(session, parent, next_node)

    def _enqueue_node(self, session: Session, parent: Job, node_id: str) -> None:
        package = parent.payload.get("package")
        if not isinstance(package, dict):
            raise AgentUpgradeConflict("stored agent upgrade package is invalid")
        self._operations.enqueue_in_session(
            session,
            parent.id,
            node_id,
            "agent.upgrade.v1",
            parent.authority_revision,
            package,
            operation_id=str(uuid.uuid4()),
        )

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
            or not 1 <= int(document["package_bytes"]) <= 1024**3
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
            raise AgentUpgradeConflict("agent upgrade package is invalid")
        return document

    @classmethod
    def _eligible(
        cls,
        node: AgentNode,
        package: Mapping[str, object],
        now: datetime,
    ) -> bool:
        return cls._ineligible_reason(node, package, now) is None

    @staticmethod
    def _ineligible_reason(
        node: AgentNode,
        package: Mapping[str, object],
        now: datetime,
    ) -> str | None:
        if node.state != "active" or node.revoked_at is not None:
            return "is not active"
        if "agent.upgrade.v1" not in set(node.capabilities or ()):
            return "does not support controller upgrades"
        if node.architecture != package["architecture"]:
            return "has an incompatible architecture"
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
            and node.self_test_passed is True
        )
