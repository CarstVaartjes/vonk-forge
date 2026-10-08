"""Models: operations."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from vonk_agent_protocol import DistributionAssignmentState, machine_check

from ..model_primitives import Base, _lower_hex


class AgentOperation(Base):
    __tablename__ = "agent_operations"
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    parent_job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    authority_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    workload_intent_ordinal: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    #: Why this operation is not currently progressing.  A lease expiry parks an
    #: operation without any attempt result, so without this the operator sees
    #: an interrupted operation and no evidence of what actually happened.
    status_reason: Mapped[str | None] = mapped_column(String(512))
    current_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: The one retry, observation and cancel clock of the lifecycle core.  On a
    #: ``waiting-for-operator`` order it is the instant its next attempt may be
    #: claimed (an automatic retry, a safety fence or an operator's resume); it is
    #: cleared by the claim.  ``NULL`` on a waiting order means a real operator
    #: wait.
    next_action_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    #: How many times the lifecycle core has looked at this waiting order since it
    #: last ran (an observation, or a stop of a cancelled order).  Above zero the
    #: order is being observed, not retried: it is not claimable and its
    #: ``next_action_at`` is when to look again.  Reset by every claim and retry.
    observe_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    #: Legacy retry encoding, replaced by ``next_action_at``.  Read only by the
    #: lifecycle adapter's ``adopt`` (and the startup adoption) for rows written
    #: before the core; nothing writes them any more.  Dropped once no deployed
    #: row can carry them.
    retry_disposition: Mapped[str | None] = mapped_column(String(32))
    retry_disposition_attempt: Mapped[int | None] = mapped_column(Integer)
    retry_due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ArtifactDistributionAssignment(Base):
    """Durable node-scoped authorization for Controller-served artifacts."""

    __tablename__ = "artifact_distribution_assignments"
    __table_args__ = (
        UniqueConstraint("plan_digest", "node_id", name="uq_distribution_plan_node"),
        CheckConstraint(
            _lower_hex("plan_digest", 64), name="ck_distribution_plan_digest"
        ),
        CheckConstraint(
            _lower_hex("model_artifact_set_sha256", 64),
            name="ck_distribution_model_set_digest",
        ),
        CheckConstraint(
            _lower_hex("oci_archive_sha256", 64), name="ck_distribution_archive_digest"
        ),
        CheckConstraint("generation >= 1", name="ck_distribution_generation"),
        CheckConstraint(
            machine_check(DistributionAssignmentState), name="ck_distribution_state"
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    model_artifact_set_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    objects: Mapped[list[dict[str, object]]] = mapped_column(JSON, nullable=False)
    oci_image_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    # The node pulls the image by manifest digest; the config digest is the
    # identity Docker reports for it on a classic image store.
    oci_image_config_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    oci_archive_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=DistributionAssignmentState.ACTIVE.value,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentOperationAttempt(Base):
    __tablename__ = "agent_operation_attempts"
    __table_args__ = (UniqueConstraint("operation_id", "attempt"),)
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    operation_id: Mapped[str] = mapped_column(
        ForeignKey("agent_operations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    fence: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    lease_deadline: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    agent_certificate_serial: Mapped[str] = mapped_column(
        ForeignKey("agent_certificates.serial"), nullable=False, index=True
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Why an ``observing`` attempt has no definite answer (``ObservationCause``).
    observation_cause: Mapped[str | None] = mapped_column(String(24))
    progress: Mapped[dict[str, object] | None] = mapped_column(JSON)
    result: Mapped[dict[str, object] | None] = mapped_column(JSON)
