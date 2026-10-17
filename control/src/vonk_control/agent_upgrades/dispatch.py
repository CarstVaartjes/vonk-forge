"""Agent upgrades: dispatch."""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC
from typing import TYPE_CHECKING, cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    AgentResult,
    LifecycleState,
)
from vonk_agent_protocol.claims import AGENT_PROTOCOL_VERSION
from vonk_agent_protocol.contracts import AgentUpgradePayload
from vonk_agent_protocol.package_upgrade import PackageRollbackAuthority

from .. import agent_operation_states, job_states
from ..agent_jobs import (
    agent_upgrade_in_flight,
)
from ..agent_upgrade_contract import (
    AgentUpgradePackage,
    AgentUpgradeRolloutResult,
)
from ..lifecycle import CancelRequested
from ..lifecycle.agent_operation import AgentOperationAdapter
from ..models import AgentNode, AgentOperation, Job
from ..strict_json import read_stored_model

if TYPE_CHECKING:
    from .service import AgentUpgradeService

from .constants import _ACTIVE_ROLLOUT_STATES, _ALREADY_CURRENT
from .helpers import _summary
from .stored import _stored_node_order, _stored_package, _stored_result, _stored_source


class DispatchMixin:
    def _advance(self, session: Session, parent: Job) -> None:
        """Own the rollout projection: dispatch, defer, or conclude.

        Every Spark gets its own order at once and upgrades independently: an
        order waits only for its own Spark, never for another one.  A Spark
        that is offline is deferred and dispatched when it polls again.  A
        Spark that already runs the target, or that can never take this
        package, is skipped, also when it drifts so while its order waits.
        Failed orders retry automatically, so the rollout concludes only when
        every target is settled.
        """
        service = cast("AgentUpgradeService", self)

        if parent.state not in _ACTIVE_ROLLOUT_STATES:
            return
        now = service._clock()
        result = _stored_result(parent)
        skipped: dict[str, str] = dict(result.skipped or {})
        deferred: list[str] = []
        operations = list(
            session.scalars(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == parent.id)
                .order_by(AgentOperation.created_at, AgentOperation.id)
            )
        )
        package = _stored_package(parent)
        order = _stored_node_order(parent)
        if result.superseded_by is None and package is not None and order is not None:
            dispatched = service._dispatch(
                session, parent, operations, package, order, skipped, deferred
            )
            service._record(parent, result, skipped)
            if dispatched:
                service._rollouts.project(parent, now, reason=None)
                return
        if any(
            operation.state == "queued"
            or agent_upgrade_in_flight(session, operation, now)
            for operation in operations
        ):
            if parent.state not in job_states.words(LifecycleState.QUEUED):
                # A legacy wait or dispatch: no action exists for a rollout,
                # and its orders are moving by themselves.
                service._rollouts.project(parent, now)
            return
        if result.superseded_by is not None:
            service._rollouts.cancelled(
                parent, now, f"superseded by agent upgrade {result.superseded_by}"
            )
            return
        if package is None or order is None:
            service._rollouts.fail(parent, now, "stored agent upgrade plan is invalid")
            return
        retrying = [
            operation
            for operation in operations
            # A dispatched order whose Spark went dark past its fence no
            # longer holds its Spark, but it is not settled either.
            if operation.state in agent_operation_states.RUNNING_OR_PARKED
        ]
        if deferred or retrying:
            service._rollouts.project(
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
            service._rollouts.fail(
                parent,
                now,
                f"Spark {failed[0].node_id} upgrade failed: "
                f"{failed[0].status_reason or 'see operation evidence'}",
            )
        else:
            service._rollouts.succeed(parent, now, reason=_summary(skipped))

    def _dispatch(
        self,
        session: Session,
        parent: Job,
        operations: list[AgentOperation],
        package: AgentUpgradePackage,
        order: list[str],
        skipped: dict[str, str],
        deferred: list[str],
    ) -> bool:
        """Queue every eligible Spark's order at once; return whether one was queued.

        A Spark that can never take the package is added to ``skipped`` (its
        never-attempted order, if any, is withdrawn); an offline one to
        ``deferred``.
        """
        service = cast("AgentUpgradeService", self)

        now = service._clock()
        materialized = {operation.node_id for operation in operations}
        never_attempted = {
            operation.node_id: operation
            for operation in operations
            if operation.state == agent_operation_states.QUEUED
            and operation.current_attempt == 0
        }
        dispatched = False
        for node_id in order:
            waiting = never_attempted.get(node_id)
            if node_id in skipped or (node_id in materialized and waiting is None):
                continue
            # Only a Spark about to be queued is locked; a waiting order's
            # Spark is merely checked for drift.
            node = (
                session.get(AgentNode, node_id)
                if waiting is not None
                else session.scalar(
                    select(AgentNode)
                    .where(AgentNode.node_id == node_id)
                    .with_for_update(of=AgentNode)
                )
            )
            if node is None:
                skipped[node_id] = "does not exist"
            elif service._at_target(node, package):
                skipped[node_id] = _ALREADY_CURRENT
            elif (
                reason := service._permanent_ineligible_reason(node, package)
            ) is not None:
                skipped[node_id] = reason
            if node is None or node_id in skipped:
                if waiting is not None:
                    # Never attempted, and this Spark can no longer take it.
                    AgentOperationAdapter(session).settle(
                        waiting,
                        None,
                        parent,
                        CancelRequested(reason=skipped[node_id]),
                        now,
                    )
                continue
            if waiting is not None:
                continue
            if service._ineligible_reason(node, package, now) is not None:
                deferred.append(node_id)
                continue
            not_queued = service._enqueue_node(session, parent, node_id)
            if not_queued is not None:
                skipped[node_id] = not_queued
                continue
            dispatched = True
        return dispatched

    @staticmethod
    def _record(
        parent: Job, result: AgentUpgradeRolloutResult, skipped: dict[str, str]
    ) -> None:
        if skipped != (result.skipped or {}):
            parent.result = result.model_copy(update={"skipped": skipped}).model_dump(
                mode="json", exclude_none=True
            )

    @classmethod
    def _contact_proves_target(
        cls,
        node: AgentNode,
        operation: AgentOperation,
        package: AgentUpgradePackage,
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

        from ..package_activation import matches_receipt

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
            and node.architecture == package.architecture
            # Signed package and binary/build digests are the compatibility
            # identity.  Version strings remain audit metadata and may differ
            # across packaging schemes without invalidating an exact upgrade.
            and node.build_digest == package.target_build_digest
            and node.binary_digest == package.target_binary_digest
            and evidence.get("architecture") == package.architecture
            and evidence.get("build_digest") == package.target_build_digest
            and evidence.get("binary_digest") == package.target_binary_digest
            and evidence.get("package_sha256") == package.package_sha256
            and evidence.get("status") == "upgraded"
        )

    def _enqueue_node(self, session: Session, parent: Job, node_id: str) -> str | None:
        """Queue the upgrade order for one Spark.

        Returns why this Spark is skipped instead (a stored package or rollback
        source that cannot be used, a Spark whose installed agent no longer matches
        its rollback source), else ``None`` once the order is queued.  The rollout
        records the reason and goes on to the next Spark; nothing is raised.
        """
        service = cast("AgentUpgradeService", self)

        package = _stored_package(parent)
        if package is None:
            return "stored agent upgrade package is invalid"
        source = _stored_source(parent, node_id)
        if source is None:
            return "stored rollback sources are invalid"
        node = session.get(AgentNode, node_id)
        if (
            node is None
            or node.binary_digest != source.package.binary_sha256
            or node.build_digest != source.build_digest
        ):
            return "rollback source no longer matches installed agent"
        payload = AgentUpgradePayload(
            **package.model_dump(mode="json"),
            source_package_bytes=source.package_bytes,
            source_package_url=source.package_url,
            rollback=PackageRollbackAuthority(
                source=source.package,
                attempt_nonce=secrets.token_hex(32),
                activation_deadline=int(service._clock().timestamp()) + 900,
            ),
        ).model_dump(mode="json")
        service._operations.enqueue_in_session(
            session,
            parent.id,
            node_id,
            "agent.upgrade.v1",
            parent.authority_revision,
            payload,
            operation_id=str(uuid.uuid4()),
        )
        return None
