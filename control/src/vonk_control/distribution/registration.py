"""Distribution: registration."""

from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING, cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    DistributionAssignmentState,
    DistributionCode,
)

from ..artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactLifecycleError,
    require_reference_open,
)
from ..artifact_reference_scan import require_model_sets_open
from ..distribution_assignment import NodeDistributionAssignment
from ..models import (
    ArtifactDistributionAssignment,
)

if TYPE_CHECKING:
    from .service import DistributionService

from .assignments import _may_replace
from .types import DistributionIntegrityError, DistributionUnknown


class RegistrationMixin:
    def attach_sessions(self, sessions: sessionmaker[Session]) -> DistributionService:
        """Bind the service to the Controller's durable assignment store."""
        service = cast("DistributionService", self)
        if service.sessions is not None and service.sessions is not sessions:
            raise RuntimeError(
                "distribution service is already bound to another database"
            )
        service.sessions = sessions
        return service

    def register(self, assignment: NodeDistributionAssignment) -> None:
        service = cast("DistributionService", self)
        assignment = NodeDistributionAssignment.parse(assignment.to_mapping())
        verifier = getattr(service.source, "verify_artifact_set", None)
        if verifier is None or not verifier(
            assignment.model_artifact_set_sha256, assignment.objects
        ):
            raise DistributionUnknown(
                DistributionCode.MODEL_SET_IDENTITY_UNAVAILABLE,
                "assignment model objects do not match a verified cache manifest",
            )
        image_verifier = getattr(service.source, "verify_runtime_image", None)
        image_verified = image_verifier is not None and image_verifier(
            assignment.oci_image_digest, assignment.oci_archive_sha256
        )
        if not image_verified:
            raise DistributionUnknown(
                DistributionCode.RUNTIME_IMAGE_MISMATCH,
                "assignment runtime image does not match the verified image identity",
            )
        key = (assignment.plan_digest, assignment.node_id)
        with service._authorized_lock:
            service._authorized.pop(key, None)
        if service.sessions is None:
            with service._lock:
                existing = service._assignments.get(key)
                if existing is not None and not _may_replace(
                    existing, assignment, active=True, now=service.clock()
                ):
                    raise DistributionIntegrityError(
                        DistributionCode.ASSIGNMENT_CONFLICT,
                        "node assignment is already bound",
                    )
                service._assignments[key] = assignment
            return
        try:
            service._register_row(assignment)
        except IntegrityError:
            # A concurrent identical registration won the unique
            # (plan_digest, node_id) constraint; the second pass compares.
            service._register_row(assignment)

    def _register_row(self, assignment: NodeDistributionAssignment) -> None:
        service = cast("DistributionService", self)
        sessions = service.sessions
        assert sessions is not None
        with sessions.begin() as session:
            row = session.scalar(
                select(ArtifactDistributionAssignment)
                .where(
                    ArtifactDistributionAssignment.plan_digest
                    == assignment.plan_digest,
                    ArtifactDistributionAssignment.node_id == assignment.node_id,
                )
                .with_for_update()
            )
            now = service.clock()
            if row is not None:
                existing = service._from_row(row)
                if existing == assignment and row.state == "active":
                    return
                if not _may_replace(
                    existing, assignment, active=row.state == "active", now=now
                ):
                    raise DistributionIntegrityError(
                        DistributionCode.ASSIGNMENT_CONFLICT,
                        "node assignment is already bound",
                    )
            try:
                require_model_sets_open(
                    session,
                    (assignment.model_artifact_set_sha256,),
                    now=now,
                )
                require_reference_open(
                    session,
                    (ArtifactIdentity("runtime-image", assignment.oci_archive_sha256),),
                    now=now,
                )
            except ArtifactLifecycleError as error:
                raise DistributionUnknown(error.code, error.detail) from error
            if row is not None and row.id != assignment.assignment_id:
                # Renewal or reclaim creates a new grant identity. Replace the
                # matching-content or inactive grant under the plan/node lock;
                # primary keys never change in place.
                session.delete(row)
                session.flush()
                row = None
            if row is not None:
                row.generation = assignment.generation
                row.expires_at = assignment.expires_at
                row.model_artifact_set_sha256 = assignment.model_artifact_set_sha256
                row.objects = [item.to_mapping() for item in assignment.objects]
                row.oci_image_digest = assignment.oci_image_digest
                row.oci_image_config_digest = assignment.oci_image_config_digest
                row.oci_archive_sha256 = assignment.oci_archive_sha256
                row.state = "active"
                row.revoked_at = None
                row.updated_at = now
                return
            session.add(
                ArtifactDistributionAssignment(
                    id=assignment.assignment_id,
                    plan_digest=assignment.plan_digest,
                    node_id=assignment.node_id,
                    generation=assignment.generation,
                    expires_at=assignment.expires_at,
                    model_artifact_set_sha256=assignment.model_artifact_set_sha256,
                    objects=[item.to_mapping() for item in assignment.objects],
                    oci_image_digest=assignment.oci_image_digest,
                    oci_image_config_digest=assignment.oci_image_config_digest,
                    oci_archive_sha256=assignment.oci_archive_sha256,
                    state=DistributionAssignmentState.ACTIVE,
                    created_at=now,
                    updated_at=now,
                )
            )

    @staticmethod
    def _from_row(row: ArtifactDistributionAssignment) -> NodeDistributionAssignment:
        return NodeDistributionAssignment.parse(
            {
                "assignment_id": row.id,
                "plan_digest": row.plan_digest,
                "generation": row.generation,
                "node_id": row.node_id,
                "expires_at": row.expires_at.replace(tzinfo=UTC).isoformat()
                if row.expires_at.tzinfo is None
                else row.expires_at.isoformat(),
                "model_artifact_set_sha256": row.model_artifact_set_sha256,
                "objects": row.objects,
                "oci_image_digest": row.oci_image_digest,
                "oci_image_config_digest": row.oci_image_config_digest,
                "oci_archive_sha256": row.oci_archive_sha256,
            }
        )

    def revoke(self, *, plan_digest: str, node_id: str) -> None:
        """Revoke a durable assignment; revocation is fail-closed on reads."""
        service = cast("DistributionService", self)
        with service._authorized_lock:
            service._authorized.pop((plan_digest, node_id), None)
        if service.sessions is None:
            with service._lock:
                service._assignments.pop((plan_digest, node_id), None)
            return
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
                row.state = "revoked"
                row.revoked_at = service.clock()
                row.updated_at = service.clock()
