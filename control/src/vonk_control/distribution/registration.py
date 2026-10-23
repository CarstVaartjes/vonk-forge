"""Distribution: registration."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC
from typing import TYPE_CHECKING, NoReturn, cast

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    DistributionAssignmentState,
    DistributionCode,
    DistributionObject,
)

from ..admission_locking import admitting
from ..artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactLifecycleError,
    require_reference_open,
)
from ..artifact_reference_scan import require_model_sets_open
from ..distribution_assignment import NodeDistributionAssignment
from ..identity_insert import insert_identity
from ..models import (
    ArtifactDistributionAssignment,
)

if TYPE_CHECKING:
    from .service import DistributionService

from .assignments import _may_replace
from .types import (
    DistributionError,
    DistributionIntegrityError,
    DistributionUnknown,
    content_difference,
)


def _refuse_model_set(
    service: DistributionService, assignment: NodeDistributionAssignment
) -> NoReturn:
    """Refuse an assignment whose model objects the stored manifest disowns.

    The verifier already re-read the stored manifest, and the manifest of a
    set digest is immutable: a mismatch is the same on every attempt, so it
    ends the load naming both sides instead of being observed again.  Only a
    manifest that cannot be read stays an unknown.
    """
    source = getattr(service.source, "model_source", service.source)
    getter = getattr(source, "objects_for_set", None)
    if not callable(getter):
        raise DistributionUnknown(
            DistributionCode.MODEL_SET_IDENTITY_UNAVAILABLE,
            "assignment model objects do not match a verified cache manifest",
        )
    declared = cast(Callable[[str], tuple[DistributionObject, ...]], getter)(
        assignment.model_artifact_set_sha256
    )
    raise DistributionError(
        DistributionCode.MODEL_SET_MISMATCH,
        "assignment model objects differ from stored manifest "
        f"{assignment.model_artifact_set_sha256[:12]}: "
        + content_difference(assignment.objects, declared),
    )


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
            _refuse_model_set(service, assignment)
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
        with admitting():
            service._register_row(assignment)

    def _register_row(self, assignment: NodeDistributionAssignment) -> None:
        service = cast("DistributionService", self)
        sessions = service.sessions
        assert sessions is not None
        with sessions.begin() as session:
            row = session.scalar(
                select(ArtifactDistributionAssignment).where(
                    ArtifactDistributionAssignment.plan_digest
                    == assignment.plan_digest,
                    ArtifactDistributionAssignment.node_id == assignment.node_id,
                )
            )
            if (
                row is not None
                and service._from_row(row) == assignment
                and row.state == DistributionAssignmentState.ACTIVE
            ):
                return
            now = service.clock()
            candidate = ArtifactDistributionAssignment(
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
            row, created = insert_identity(
                session,
                candidate,
                key=("plan_digest", "node_id"),
            )
            if not created:
                existing = service._from_row(row)
                if (
                    existing == assignment
                    and row.state == DistributionAssignmentState.ACTIVE
                ):
                    return
                if not _may_replace(
                    existing,
                    assignment,
                    active=row.state == DistributionAssignmentState.ACTIVE,
                    now=now,
                ):
                    raise DistributionIntegrityError(
                        DistributionCode.ASSIGNMENT_CONFLICT,
                        "node assignment is already bound",
                    )
                # Renewal/reclaim replaces only the exact generation just compared.
                # Revocation or another renewal wins rather than being overwritten.
                removed = session.scalar(
                    delete(ArtifactDistributionAssignment)
                    .where(
                        ArtifactDistributionAssignment.id == row.id,
                        ArtifactDistributionAssignment.generation == row.generation,
                        ArtifactDistributionAssignment.expires_at == row.expires_at,
                        ArtifactDistributionAssignment.state == row.state,
                        ArtifactDistributionAssignment.updated_at == row.updated_at,
                        ArtifactDistributionAssignment.revoked_at == row.revoked_at,
                    )
                    .returning(ArtifactDistributionAssignment.id)
                )
                if removed is not None:
                    _, created = insert_identity(
                        session, candidate, key=("plan_digest", "node_id")
                    )
                if removed is None:
                    current = session.scalar(
                        select(ArtifactDistributionAssignment)
                        .where(
                            ArtifactDistributionAssignment.plan_digest
                            == assignment.plan_digest,
                            ArtifactDistributionAssignment.node_id
                            == assignment.node_id,
                        )
                        .execution_options(populate_existing=True)
                    )
                    if (
                        current is not None
                        and service._from_row(current) == assignment
                        and current.state == DistributionAssignmentState.ACTIVE
                    ):
                        return
                if removed is None or not created:
                    raise DistributionError(
                        DistributionCode.ASSIGNMENT_CONFLICT,
                        "node assignment changed during registration",
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
