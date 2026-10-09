"""Agent upgrades: acceptance."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, cast

import httpx2
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from vonk_agent_protocol import (
    InvalidRequestReason,
)
from vonk_agent_protocol.package_source import AgentPackageSource

from ..agent_package_source import load_package_source
from ..agent_upgrade_contract import (
    AgentUpgradePackage,
    AgentUpgradeRepairManifest,
    AgentUpgradeRequestIntent,
    AgentUpgradeRolloutPayload,
    AgentUpgradeRolloutResult,
)
from ..lifecycle.agent_upgrade import AgentUpgradeAdapter
from ..models import AgentNode, Job

if TYPE_CHECKING:
    from .service import AgentUpgradeService

from .constants import _ALREADY_CURRENT
from .errors import AgentUpgradeConflict, AgentUpgradeInvalid
from .helpers import _summary
from .intent import _plan_digest, _request_intent, _rollout_document
from .plan import AgentUpgradePlan


class AcceptanceMixin:
    def preview(
        self,
        node_ids: Sequence[str] | None,
        package: AgentUpgradePackage,
        *,
        repair_manifest: AgentUpgradeRepairManifest | None = None,
        request_intent: AgentUpgradeRequestIntent | None = None,
    ) -> AgentUpgradePlan:
        service = cast("AgentUpgradeService", self)
        payload = package
        intent = _request_intent(request_intent, node_ids)
        repair = (
            None
            if repair_manifest is None
            else service._repair_manifest(repair_manifest, payload)
        )
        if repair is not None and (
            node_ids is None or tuple(node_ids) != (repair.node_id,)
        ):
            raise AgentUpgradeInvalid(
                "agent repair requires exactly its explicit Spark",
                reason=InvalidRequestReason.INCOMPLETE,
            )
        # The agent wire requires a 64-hex authority revision on every
        # upgrade operation; the signed package digest names what it installs.
        authority_revision = payload.package_sha256
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
        with service._sessions() as session:
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
                elif service._at_target(node, payload):
                    skipped[node_id] = _ALREADY_CURRENT
                elif (
                    reason := service._permanent_ineligible_reason(node, payload)
                ) is not None:
                    skipped[node_id] = reason
                else:
                    # An offline Spark stays in the rollout: it is deferred and
                    # upgraded automatically when it reconnects.
                    installed.append(
                        (node_id, node.build_digest or "", node.binary_digest or "")
                    )
        sources: dict[str, AgentPackageSource] = {}
        for node_id, build_digest, binary_digest in installed:
            try:
                source = load_package_source(
                    service._http, service._channel, build_digest, binary_digest
                )
            except (httpx2.HTTPError, ValueError):
                skipped[node_id] = "has no published signed rollback package"
                continue
            sources[node_id] = source
        targets = tuple(node_id for node_id, _, _ in installed if node_id in sources)
        return AgentUpgradePlan(
            authority_revision=authority_revision,
            node_ids=targets,
            package=payload,
            plan_digest=_plan_digest(
                sources=sources,
                authority_revision=authority_revision,
                node_ids=list(targets),
                package=payload,
                request_intent=intent,
                repair_manifest=repair,
            ),
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
        request_intent: AgentUpgradeRequestIntent,
    ) -> Job | None:
        """Return the exact durable job for a repeated fleet-upgrade request."""
        service = cast("AgentUpgradeService", self)

        intent = _request_intent(request_intent, None)
        with service._sessions() as session:
            job = session.scalar(select(Job).where(Job.request_id == request_id))
            if job is None:
                return None
            service._check_replayed_request(job, actor, intent)
            session.expunge(job)
            return job

    @staticmethod
    def _check_replayed_request(
        job: Job, actor: str, request_intent: AgentUpgradeRequestIntent
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
        package: AgentUpgradePackage,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        repair_manifest: AgentUpgradeRepairManifest | None = None,
        request_intent: AgentUpgradeRequestIntent | None = None,
    ) -> Job:
        service = cast("AgentUpgradeService", self)
        intent = _request_intent(request_intent, node_ids)
        existing = service.get_request(request_id, actor=actor, request_intent=intent)
        if existing is not None:
            return existing
        plan = service.preview(
            node_ids,
            package,
            repair_manifest=repair_manifest,
            request_intent=intent,
        )
        # The latest request leads: a preview digest that no longer matches is
        # advisory.  The freshly computed plan for the same intent is applied.
        del plan_digest
        now = service._clock()
        job = AgentUpgradeAdapter.new_rollout(
            allowed=bool(plan.node_ids),
            request_id=request_id,
            kind="agent-upgrade",
            status_reason=None if plan.node_ids else _summary(plan.skipped),
            result=(
                AgentUpgradeRolloutResult(skipped=dict(plan.skipped)).model_dump(
                    mode="json", exclude_none=True
                )
                if plan.skipped
                else None
            ),
            actor=actor,
            authority_revision=plan.authority_revision,
            targets=list(plan.node_ids),
            payload_digest=plan.plan_digest,
            payload=_rollout_document(
                AgentUpgradeRolloutPayload(
                    node_order=list(plan.node_ids),
                    package=plan.package,
                    request_intent=plan.request_intent,
                    sources=plan.sources,
                    repair_manifest=plan.repair_manifest,
                )
            ),
            current_attempt=0,
            created_at=now,
            updated_at=now,
        )
        try:
            with service._sessions.begin() as session:
                existing = session.scalar(
                    select(Job).where(Job.request_id == request_id).with_for_update()
                )
                if existing is not None:
                    service._check_replayed_request(existing, actor, intent)
                    session.expunge(existing)
                    return existing
                session.add(job)
                session.flush()
                service._supersede_older(session, job, now)
                if plan.node_ids:
                    service._advance(session, job)
        except IntegrityError:
            existing = service.get_request(
                request_id, actor=actor, request_intent=intent
            )
            if existing is not None:
                return existing
            raise
        service._operations.notify_available()
        return job
