"""Distribution: delivery."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, timedelta
from time import monotonic
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from sqlalchemy import select
from vonk_agent_protocol import (
    AgentOperation as OperationKind,
)
from vonk_agent_protocol import (
    DistributionAssignmentState,
    DistributionCode,
    DistributionObject,
    LifecycleState,
    SecurityRefusalError,
    SecurityRefusalReason,
)
from vonk_agent_protocol.recipe_jobs import RecipeJobRunRequest
from vonk_agent_protocol.recipe_operations import (
    RecipeInstallPayload,
    RecipeStartPayload,
)

from ..distribution_assignment import NodeDistributionAssignment
from ..models import (
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    ArtifactDistributionAssignment,
    Job,
)

if TYPE_CHECKING:
    from .service import DistributionService

from .constants import _AUTHORIZATION_CACHE_ENTRIES, _AUTHORIZATION_TTL_SECONDS
from .locations import _LOCATION_CACHE_ENTRIES, ObjectLocation, _still_stored
from .types import (
    DistributionError,
    DistributionRefused,
    DistributionUnknown,
    OpenedObject,
)


class DeliveryMixin:
    def prepare_request_delivery(self, *, node_id: str, plan_digest: str) -> None:
        """Grant exact cached assets to an accepted, currently leased request.

        Installation and execution use their own plan digest. They must not
        depend on a previous profile's distribution grant or operator prewarm.
        Storage verification happens after releasing the database session.
        """
        service = cast("DistributionService", self)
        from ..agent_jobs.stored import column_field, column_value

        if service.sessions is None:
            return
        now = service.clock()
        accepted = None
        with service.sessions() as session:
            rows = session.execute(
                select(AgentOperation, AgentOperationAttempt, Job, AgentNode)
                .join(
                    AgentOperationAttempt,
                    (AgentOperationAttempt.operation_id == AgentOperation.id)
                    & (AgentOperationAttempt.attempt == AgentOperation.current_attempt),
                )
                .join(Job, Job.id == AgentOperation.parent_job_id)
                .join(AgentNode, AgentNode.node_id == AgentOperation.node_id)
                .where(
                    AgentOperation.node_id == node_id,
                    AgentOperation.state == LifecycleState.RUNNING.value,
                    AgentOperation.kind.in_(
                        (
                            OperationKind.RECIPE_INSTALL.value,
                            OperationKind.RECIPE_START.value,
                            OperationKind.RECIPE_JOB_RUN.value,
                        )
                    ),
                    AgentOperationAttempt.state == LifecycleState.RUNNING.value,
                    AgentOperationAttempt.lease_deadline > now,
                    Job.state.in_(
                        (LifecycleState.QUEUED.value, LifecycleState.RUNNING.value)
                    ),
                    AgentNode.revoked_at.is_(None),
                )
            )
            for operation, _attempt, parent, node in rows:
                payload = column_value(operation, "payload")
                if (
                    isinstance(
                        payload,
                        RecipeInstallPayload | RecipeStartPayload | RecipeJobRunRequest,
                    )
                    and payload.plan_digest == plan_digest
                    and operation.authority_revision == parent.authority_revision
                    and node_id in parent.targets
                    and column_field(parent, "result", "cancel_requested") is not True
                    and operation.workload_intent_ordinal is not None
                    and operation.workload_intent_ordinal > 0
                    and operation.workload_intent_ordinal
                    == node.workload_intent_ordinal
                    and operation.workload_intent_ordinal
                    == column_field(parent, "payload", "workload_intent_ordinal")
                ):
                    accepted = payload.compiled_execution_plan
                    break
        if accepted is None:
            return
        # The managed cache owns object names and membership; the accepted
        # plan binds the complete manifest and exact image content identity.
        source = getattr(service.source, "model_source", service.source)
        getter = getattr(source, "objects_for_set", None)
        if not callable(getter):
            raise DistributionUnknown(
                DistributionCode.MODEL_SET_IDENTITY_UNAVAILABLE,
                "accepted model manifest is not currently observable",
            )
        objects = cast(Callable[[str], tuple[DistributionObject, ...]], getter)(
            accepted.identity.model_artifact_set_sha256
        )
        delivered = {item.sha256: item.bytes for item in objects}
        if any(
            delivered.get(item.sha256) != item.size_bytes for item in accepted.artifacts
        ):
            raise DistributionUnknown(
                DistributionCode.MODEL_SET_IDENTITY_UNAVAILABLE,
                "accepted model objects are not currently observable",
            )
        image = accepted.runtime_image
        service.register(
            NodeDistributionAssignment(
                assignment_id=str(uuid4()),
                plan_digest=plan_digest,
                generation=1,
                node_id=node_id,
                expires_at=now + timedelta(hours=1),
                model_artifact_set_sha256=accepted.identity.model_artifact_set_sha256,
                objects=objects,
                oci_image_digest=image.image_digest,
                oci_image_config_digest=image.local_image_config_id,
                oci_archive_sha256=image.oci_layout_sha256,
            )
        )

    def authorize(
        self, *, node_id: str, plan_digest: str
    ) -> NodeDistributionAssignment:
        """Decide once per assignment, not once per range request.

        A positive decision is remembered for a short, bounded time and never
        past the assignment's own expiry. There is no process-wide lock: the
        database arbitrates, and the cache only holds immutable assignments.
        """
        service = cast("DistributionService", self)
        key = (plan_digest, node_id)
        now = service.clock()
        if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
            raise DistributionUnknown(
                DistributionCode.OBJECT_UNAVAILABLE,
                "distribution authorization clock is unavailable",
            )
        with service._authorized_lock:
            cached = service._authorized.get(key)
            if cached is not None:
                assignment, decided_at = cached
                if (
                    monotonic() - decided_at < _AUTHORIZATION_TTL_SECONDS
                    and assignment.expires_at > now
                ):
                    return assignment
                del service._authorized[key]
        assignment = service._authorize_uncached(
            node_id=node_id, plan_digest=plan_digest
        )
        with service._authorized_lock:
            service._authorized[key] = (assignment, monotonic())
            while len(service._authorized) > _AUTHORIZATION_CACHE_ENTRIES:
                service._authorized.popitem(last=False)
        return assignment

    def _authorize_uncached(
        self, *, node_id: str, plan_digest: str
    ) -> NodeDistributionAssignment:
        service = cast("DistributionService", self)
        if service.sessions is None:
            assignment = service._assignments.get((plan_digest, node_id))
            plan_assignment = next(
                (
                    item
                    for (digest, _node), item in list(service._assignments.items())
                    if digest == plan_digest
                ),
                None,
            )
        else:
            with service.sessions() as session:
                row = session.scalar(
                    select(ArtifactDistributionAssignment).where(
                        ArtifactDistributionAssignment.plan_digest == plan_digest,
                        ArtifactDistributionAssignment.node_id == node_id,
                    )
                )
                if row is None:
                    assignment = None
                    plan_assignment = session.scalar(
                        select(ArtifactDistributionAssignment).where(
                            ArtifactDistributionAssignment.plan_digest == plan_digest,
                        )
                    )
                elif row.state != "active":
                    raise DistributionRefused(
                        SecurityRefusalReason.DISTRIBUTION_REVOKED.value,
                        "assignment is no longer active",
                        reason=SecurityRefusalReason.DISTRIBUTION_REVOKED,
                    )
                else:
                    assignment = service._from_row(row)
                    plan_assignment = assignment
        if assignment is None:
            if plan_assignment is not None:
                raise DistributionError(
                    DistributionCode.WRONG_NODE, "assignment is bound to another node"
                )
            raise DistributionError(
                DistributionCode.UNASSIGNED, "assignment is not available"
            )
        if assignment.node_id != node_id:
            raise DistributionError(
                DistributionCode.WRONG_NODE, "assignment is bound to another node"
            )
        now = service.clock()
        if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
            raise DistributionUnknown(
                DistributionCode.OBJECT_UNAVAILABLE,
                "distribution authorization clock is unavailable",
            )
        if assignment.expires_at <= now:
            if service.sessions is not None:
                with service.sessions.begin() as session:
                    row = session.scalar(
                        select(ArtifactDistributionAssignment)
                        .where(
                            ArtifactDistributionAssignment.plan_digest == plan_digest,
                            ArtifactDistributionAssignment.node_id == node_id,
                        )
                        .with_for_update()
                    )
                    if row is not None:
                        row.state = DistributionAssignmentState.EXPIRED
                        row.updated_at = now
            raise DistributionError(DistributionCode.EXPIRED, "assignment has expired")
        return assignment

    def locate_object(
        self, *, node_id: str, plan_digest: str, digest: str
    ) -> tuple[NodeDistributionAssignment, DistributionObject, ObjectLocation]:
        """Authorize one object and name its stored file for the edge to serve.

        Same decision as :meth:`open_object` without keeping a file open. The
        authorization is always re-read (itself cached per assignment); only
        the object's storage resolution is remembered for a short time.
        """
        service = cast("DistributionService", self)
        assignment = service.authorize(node_id=node_id, plan_digest=plan_digest)
        object_spec = next(
            (item for item in assignment.objects if item.sha256 == digest), None
        )
        key = (plan_digest, node_id, digest)
        if object_spec is not None:
            with service._authorized_lock:
                hit = service._located.get(key)
                if hit is not None and (
                    monotonic() - hit[2] >= _AUTHORIZATION_TTL_SECONDS
                    or hit[0] != object_spec
                ):
                    del service._located[key]
                    hit = None
            if hit is not None and _still_stored(hit[1], object_spec.bytes):
                return assignment, object_spec, hit[1]
        _assignment, object_spec, opened = service.open_object(
            node_id=node_id, plan_digest=plan_digest, digest=digest
        )
        opened.stream.close()
        location = ObjectLocation(opened.size, opened.sha256, opened.path)
        with service._authorized_lock:
            service._located[key] = (object_spec, location, monotonic())
            while len(service._located) > _LOCATION_CACHE_ENTRIES:
                service._located.popitem(last=False)
        return assignment, object_spec, location

    def open_object(
        self, *, node_id: str, plan_digest: str, digest: str
    ) -> tuple[NodeDistributionAssignment, DistributionObject, OpenedObject]:
        service = cast("DistributionService", self)
        assignment = service.authorize(node_id=node_id, plan_digest=plan_digest)
        object_spec = next(
            (item for item in assignment.objects if item.sha256 == digest), None
        )
        if object_spec is None:
            raise DistributionError(
                DistributionCode.UNASSIGNED, "object is not assigned to this node"
            )
        # The worker registers assignments, while another API process serves
        # their bytes. Rehydrate that process's model lookup from the durable
        # assignment instead of relying on the worker's in-memory cache.
        if object_spec.kind == "model" and not service.source.verify_artifact_set(
            assignment.model_artifact_set_sha256, assignment.objects
        ):
            raise DistributionUnknown(
                DistributionCode.MODEL_SET_IDENTITY_UNAVAILABLE,
                "assignment model objects do not match the cache manifest",
            )
        try:
            opened = service.source.open_object(digest, object_spec.bytes)
        except DistributionError:
            raise
        except SecurityRefusalError:
            raise
        except Exception as error:
            raise DistributionUnknown(
                DistributionCode.OBJECT_UNAVAILABLE, "stored object is unavailable"
            ) from error
        if opened.size != object_spec.bytes or opened.sha256 != digest:
            opened.stream.close()
            raise DistributionUnknown(
                DistributionCode.OBJECT_UNAVAILABLE, "source returned an invalid object"
            )
        return assignment, object_spec, opened
