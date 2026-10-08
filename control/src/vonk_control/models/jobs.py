"""Models: jobs."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..model_primitives import Base, _lower_hex


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    request_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    authority_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    targets: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    result: Mapped[dict[str, object] | None] = mapped_column(JSON)
    status_reason: Mapped[str | None] = mapped_column(Text)
    current_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class JobAttempt(Base):
    __tablename__ = "job_attempts"
    __table_args__ = (UniqueConstraint("job_id", "attempt"),)
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    fence: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(200), nullable=False)
    lease_deadline: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Why an ``observing`` attempt has no definite answer (``ObservationCause``).
    observation_cause: Mapped[str | None] = mapped_column(String(24))


class ControlProcessHeartbeat(Base):
    """A completed scheduler loop bound to one running worker process."""

    __tablename__ = "control_process_heartbeats"
    __table_args__ = (
        CheckConstraint(
            _lower_hex("source_sha", 40), name="ck_control_process_heartbeats_source"
        ),
        CheckConstraint(
            _lower_hex("worker_contract_sha256", 64),
            name="ck_control_process_heartbeats_worker_contract",
        ),
        UniqueConstraint(
            "process_kind",
            "process_instance_id",
            name="uq_control_process_heartbeats_instance",
        ),
        CheckConstraint(
            "process_kind = 'worker'",
            name="ck_control_process_heartbeats_process_kind",
        ),
        CheckConstraint(
            _lower_hex("process_instance_id", 64),
            name="ck_control_process_heartbeats_process_instance_id",
        ),
        CheckConstraint(
            "(loop_sequence = 0 AND completed_at IS NULL) OR "
            "(loop_sequence >= 1 AND completed_at IS NOT NULL)",
            name="ck_control_process_heartbeats_loop_sequence",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    process_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    process_instance_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    worker_contract_sha256: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    loop_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
