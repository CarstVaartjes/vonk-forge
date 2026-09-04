"""Local catalog authority and operational database state.

PostgreSQL is authoritative for recipes, revisions, placement, runtime state,
Fleet metadata, and Library catalog state.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    event,
    inspect,
    literal_column,
    select,
    text,
)
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship
from sqlalchemy.sql.functions import FunctionElement


class Base(DeclarativeBase):
    pass


class _Utf8ByteLength(FunctionElement[int]):
    type = Integer()
    inherit_cache = True


@compiles(_Utf8ByteLength, "sqlite")
def _compile_sqlite_utf8_byte_length(element, compiler, **kwargs) -> str:
    value = compiler.process(next(iter(element.clauses)), **kwargs)
    return f"length(CAST({value} AS BLOB))"


@compiles(_Utf8ByteLength)
def _compile_utf8_byte_length(element, compiler, **kwargs) -> str:
    value = compiler.process(next(iter(element.clauses)), **kwargs)
    return f"octet_length(CAST({value} AS TEXT))"


def _lower_hex(column: str, length: int) -> str:
    remainder = column
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return (
        f"length({column}) = {length} AND {column} = lower({column}) AND "
        f"length({remainder}) = 0"
    )


def _nullable_lower_hex(column: str, length: int) -> str:
    return f"{column} IS NULL OR ({_lower_hex(column, length)})"


def _uuid_shape(column: str) -> str:
    compact = f"replace({column}, '-', '')"
    return (
        f"length({column}) = 36 AND substr({column}, 9, 1) = '-' AND "
        f"substr({column}, 14, 1) = '-' AND substr({column}, 19, 1) = '-' AND "
        f"substr({column}, 24, 1) = '-' AND ({_lower_hex(compact, 32)})"
    )


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
    reconciliation_id: Mapped[str | None] = mapped_column(
        ForeignKey("reconciliations.id"), unique=True, index=True
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


class JobLogEntry(Base):
    """Redacted content-addressed job log stored in PostgreSQL."""

    __tablename__ = "job_log_entries"
    job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    request_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    authority_revision: Mapped[str | None] = mapped_column(String(128))
    targets: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class ControlAuthorityRevision(Base):
    """Immutable control-plane authority document revision in PostgreSQL."""

    __tablename__ = "control_authority_revisions"
    revision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    parent_revision: Mapped[str | None] = mapped_column(
        ForeignKey("control_authority_revisions.revision_id"), index=True
    )
    documents: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    dependencies: Mapped[dict[str, list[str]]] = mapped_column(JSON, nullable=False)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class ControlAuthorityHead(Base):
    """Singleton pointer to the current immutable authority revision."""

    __tablename__ = "control_authority_heads"
    singleton_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision_id: Mapped[str] = mapped_column(
        ForeignKey("control_authority_revisions.revision_id"),
        nullable=False,
        unique=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ControlAuthorityProposal(Base):
    """Persisted proposal preview, allowing submission after API restart."""

    __tablename__ = "control_authority_proposals"
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    base_revision: Mapped[str] = mapped_column(
        ForeignKey("control_authority_revisions.revision_id"),
        nullable=False,
        index=True,
    )
    changes: Mapped[list[dict[str, object]]] = mapped_column(JSON, nullable=False)
    patch: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    affected_documents: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    validation_results: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    applied_revision: Mapped[str | None] = mapped_column(
        ForeignKey("control_authority_revisions.revision_id"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class Observation(Base):
    __tablename__ = "observations"
    __table_args__ = (
        Index(
            "ix_observations_kind_node_observed",
            "kind",
            "node_id",
            "observed_at",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    node_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class ControlProcessHeartbeat(Base):
    """A completed scheduler loop bound to one running worker process."""

    __tablename__ = "control_process_heartbeats"
    __table_args__ = (
        UniqueConstraint("process_kind", name="uq_control_process_heartbeats_kind"),
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
    loop_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class Reconciliation(Base):
    __tablename__ = "reconciliations"
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    authority_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    graph: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        default=lambda: {
            "authority_revision": "",
            "nodes": [],
            "schema_version": 1,
            "targets": [],
        },
        server_default='{"authority_revision":"","nodes":[],"schema_version":1,"targets":[]}',
    )
    graph_digest: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="5c061eb8dfce0a3f2bcbfbf06cb71d695c33e8f4269e17bfe5cd1cda0054cdc5",
        server_default="5c061eb8dfce0a3f2bcbfbf06cb71d695c33e8f4269e17bfe5cd1cda0054cdc5",
    )
    plan_digest: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    resolved_plan: Mapped[dict[str, object] | None] = mapped_column(JSON)
    current_phase: Mapped[str] = mapped_column(
        String(32), nullable=False, default="planned", server_default="planned"
    )
    route_withdrawal_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    terminal_reason: Mapped[str | None] = mapped_column(Text)
    completion_generation: Mapped[int | None] = mapped_column(
        BigInteger, unique=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class ReconciliationCompletionGeneration(Base):
    __tablename__ = "reconciliation_completion_generation"
    singleton_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)


class ReconciliationOperation(Base):
    __tablename__ = "reconciliation_operations"
    __table_args__ = (
        UniqueConstraint(
            "reconciliation_id",
            "graph_operation_id",
            "role",
            name="uq_reconciliation_operation_graph_role",
        ),
        CheckConstraint(
            "length(graph_operation_id) BETWEEN 1 AND 128",
            name="ck_reconciliation_operations_graph_operation_id_length",
        ),
        CheckConstraint(
            "role IN ('primary', 'compensation')",
            name="ck_reconciliation_operations_role",
        ),
        CheckConstraint(
            "state IN ('planned', 'queued', 'running', 'succeeded', "
            "'accepted', 'failed', 'waiting-for-operator', 'compensating', "
            "'compensated', 'uncertain')",
            name="ck_reconciliation_operations_state",
        ),
        CheckConstraint(
            "length(expected_payload_digest) = 64",
            name="ck_reconciliation_operations_expected_payload_digest_length",
        ),
        CheckConstraint(
            "result_digest IS NULL OR length(result_digest) = 64",
            name="ck_reconciliation_operations_result_digest_length",
        ),
        CheckConstraint(
            "evidence_digest IS NULL OR length(evidence_digest) = 64",
            name="ck_reconciliation_operations_evidence_digest_length",
        ),
        CheckConstraint(
            "compensated_graph_operation_id IS NULL OR "
            "length(compensated_graph_operation_id) BETWEEN 1 AND 128",
            name="ck_reconciliation_operations_compensated_id_length",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    reconciliation_id: Mapped[str] = mapped_column(
        ForeignKey("reconciliations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    graph_operation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    agent_operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_operations.id"), unique=True, index=True
    )
    expected_payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    result_digest: Mapped[str | None] = mapped_column(String(64))
    evidence_digest: Mapped[str | None] = mapped_column(String(64))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    compensated_graph_operation_id: Mapped[str | None] = mapped_column(String(128))


class ReconciliationCancellation(Base):
    """Durable operator intent advanced independently of process lifetime."""

    __tablename__ = "reconciliation_cancellations"
    __table_args__ = (
        CheckConstraint(
            "state IN ('requested', 'withdrawal-pending', 'withdrawn', "
            "'processing', 'compensating', 'completed', "
            "'waiting-for-operator')",
            name="ck_reconciliation_cancellations_state",
        ),
        CheckConstraint(
            "length(reason) BETWEEN 1 AND 1024",
            name="ck_reconciliation_cancellations_reason_length",
        ),
    )
    reconciliation_id: Mapped[str] = mapped_column(
        ForeignKey("reconciliations.id", ondelete="CASCADE"), primary_key=True
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class RoutePublication(Base):
    __tablename__ = "route_publications"
    __table_args__ = (
        CheckConstraint(
            "state IN ('withdrawal-pending', 'routes-withdrawn', "
            "'publication-pending', 'completed', 'failed')",
            name="ck_route_publications_state",
        ),
        CheckConstraint(
            "generation IS NULL OR generation >= 0",
            name="ck_route_publications_generation",
        ),
        CheckConstraint(
            "length(plan_digest) = 64",
            name="ck_route_publications_plan_digest_length",
        ),
        CheckConstraint(
            "evidence_digest IS NULL OR length(evidence_digest) = 64",
            name="ck_route_publications_evidence_digest_length",
        ),
        CheckConstraint(
            "route_digest IS NULL OR length(route_digest) = 64",
            name="ck_route_publications_route_digest_length",
        ),
        CheckConstraint(
            "litellm_digest IS NULL OR length(litellm_digest) = 64",
            name="ck_route_publications_litellm_digest_length",
        ),
        CheckConstraint(
            "bundle_digest IS NULL OR length(bundle_digest) = 64",
            name="ck_route_publications_bundle_digest_length",
        ),
        CheckConstraint(
            "activation_marker_digest IS NULL OR length(activation_marker_digest) = 64",
            name="ck_route_publications_activation_marker_digest_length",
        ),
        CheckConstraint(
            "lease_expires_at IS NULL OR "
            "(lease_issued_at IS NOT NULL AND lease_expires_at > lease_issued_at)",
            name="ck_route_publications_lease_window",
        ),
    )
    reconciliation_id: Mapped[str] = mapped_column(
        ForeignKey("reconciliations.id", ondelete="CASCADE"), primary_key=True
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    generation: Mapped[int | None] = mapped_column(BigInteger, unique=True, index=True)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_digest: Mapped[str | None] = mapped_column(String(64))
    route_digest: Mapped[str | None] = mapped_column(String(64))
    litellm_digest: Mapped[str | None] = mapped_column(String(64))
    bundle_digest: Mapped[str | None] = mapped_column(String(64))
    activation_marker: Mapped[dict[str, object] | None] = mapped_column(JSON)
    activation_marker_digest: Mapped[str | None] = mapped_column(String(64))
    lease_issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RoutePublicationOwner(Base):
    """Singleton authority for the one global LiteLLM activation marker."""

    __tablename__ = "route_publication_owner"
    __table_args__ = (
        CheckConstraint(
            "singleton_id = 1",
            name="ck_route_publication_owner_singleton",
        ),
        CheckConstraint(
            "owner_generation >= 0",
            name="ck_route_publication_owner_generation",
        ),
    )
    singleton_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reconciliation_id: Mapped[str | None] = mapped_column(
        ForeignKey("reconciliations.id", ondelete="SET NULL"),
        unique=True,
    )
    owner_generation: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    subject: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_verifier: Mapped[str | None] = mapped_column(String(255), nullable=True)


class LoginSession(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentProfile(Base):
    __tablename__ = "agent_profiles"
    node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id", ondelete="CASCADE"), primary_key=True
    )
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    lifecycle: Mapped[str] = mapped_column(
        String(16), nullable=False, default="managed"
    )
    labels: Mapped[dict[str, object]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class AgentNode(Base):
    __tablename__ = "agent_nodes"
    __table_args__ = (
        CheckConstraint(
            "architecture IS NULL OR architecture IN ('linux-amd64', 'linux-arm64')",
            name="ck_agent_nodes_architecture",
        ),
        CheckConstraint(
            _nullable_lower_hex("observation_receipt_public_key", 64),
            name="ck_agent_nodes_observation_receipt_public_key",
        ),
    )
    node_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    protocol_version: Mapped[int | None] = mapped_column(Integer)
    architecture: Mapped[str | None] = mapped_column(String(16))
    semantic_version: Mapped[str | None] = mapped_column(String(32))
    build_digest: Mapped[str | None] = mapped_column(String(71))
    binary_digest: Mapped[str | None] = mapped_column(String(64))
    self_test_passed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    observation_receipt_public_key: Mapped[str | None] = mapped_column(String(64))
    contact_certificate_serial: Mapped[str | None] = mapped_column(String(128))
    contact_observation_digest: Mapped[str | None] = mapped_column(String(64))
    capabilities: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentNodeProfile(Base):
    """Mutable display metadata for an enrolled agent node."""

    __tablename__ = "agent_node_profiles"
    __table_args__ = (
        CheckConstraint(
            "length(display_name) BETWEEN 1 AND 200",
            name="ck_agent_node_profiles_display_name_length",
        ),
        CheckConstraint(
            "length(hostname) <= 255",
            name="ck_agent_node_profiles_hostname_length",
        ),
        CheckConstraint(
            "length(lifecycle) BETWEEN 1 AND 64",
            name="ck_agent_node_profiles_lifecycle_length",
        ),
    )
    node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id", ondelete="CASCADE"), primary_key=True
    )
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    lifecycle: Mapped[str] = mapped_column(
        String(64), nullable=False, default="managed", server_default="managed"
    )
    labels: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)


class FleetProfile(Base):
    """Named complete desired recipe placements for a set of Sparks."""

    __tablename__ = "fleet_profiles"
    __table_args__ = (
        CheckConstraint(
            "length(name) BETWEEN 1 AND 120",
            name="ck_fleet_profiles_name_length",
        ),
        CheckConstraint(
            "length(description) <= 1000",
            name="ck_fleet_profiles_description_length",
        ),
        CheckConstraint(
            "installation_policy IN ('keep-cached','exact')",
            name="ck_fleet_profiles_installation_policy",
        ),
        CheckConstraint(
            "length(CAST(assignments AS TEXT)) BETWEEN 2 AND 131072",
            name="ck_fleet_profiles_assignments_size",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(
        String(1000), nullable=False, default="", server_default=""
    )
    installation_policy: Mapped[str] = mapped_column(
        String(24), nullable=False, default="keep-cached", server_default="keep-cached"
    )
    assignments: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    labels: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    favorite: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class FleetProfileApplication(Base):
    """Restart-safe application of one digest-bound Fleet profile preview."""

    __tablename__ = "fleet_profile_applications"
    __table_args__ = (
        CheckConstraint(
            "state IN ('queued','running','waiting-for-operator','succeeded','failed','cancelled')",
            name="ck_fleet_profile_applications_state",
        ),
        CheckConstraint(
            _lower_hex("profile_digest", 64),
            name="ck_fleet_profile_applications_profile_digest",
        ),
        CheckConstraint(
            _lower_hex("plan_digest", 64),
            name="ck_fleet_profile_applications_plan_digest",
        ),
        CheckConstraint(
            "current_step >= 0",
            name="ck_fleet_profile_applications_current_step",
        ),
        CheckConstraint(
            "length(CAST(plan AS TEXT)) BETWEEN 2 AND 262144",
            name="ck_fleet_profile_applications_plan_size",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    request_key: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    profile_id: Mapped[str] = mapped_column(
        ForeignKey("fleet_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    profile_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    plan: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    current_step: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_operation_id: Mapped[str | None] = mapped_column(String(36), index=True)
    progress: Mapped[dict[str, object]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    result: Mapped[dict[str, object] | None] = mapped_column(JSON)
    status_reason: Mapped[str | None] = mapped_column(String(512))
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class NodeMutationLease(Base):
    """Exclusive durable ownership of one node's mutations and route state."""

    __tablename__ = "node_mutation_leases"
    __table_args__ = (
        CheckConstraint(
            "owner_kind = 'reconciliation'",
            name="ck_node_mutation_leases_owner_kind",
        ),
        CheckConstraint(
            "state IN ('held', 'releasing')",
            name="ck_node_mutation_leases_state",
        ),
        CheckConstraint(
            _uuid_shape("owner_id"),
            name="ck_node_mutation_leases_owner_id_shape",
        ),
        CheckConstraint(
            _uuid_shape("fence"),
            name="ck_node_mutation_leases_fence_shape",
        ),
        CheckConstraint(
            "updated_at >= acquired_at",
            name="ck_node_mutation_leases_timestamp_order",
        ),
        Index(
            "ix_node_mutation_leases_owner",
            "owner_kind",
            "owner_id",
        ),
    )
    node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id", ondelete="CASCADE"), primary_key=True
    )
    owner_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(36), nullable=False)
    fence: Mapped[str] = mapped_column(String(36), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class AgentCertificate(Base):
    __tablename__ = "agent_certificates"
    __table_args__ = (UniqueConstraint("node_id", "generation"),)
    serial: Mapped[str] = mapped_column(String(128), primary_key=True)
    node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id"), nullable=False, index=True
    )
    not_before: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    not_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    certificate_pem: Mapped[str | None] = mapped_column(Text)
    chain_pem: Mapped[str | None] = mapped_column(Text)
    csr_public_key_fingerprint: Mapped[str | None] = mapped_column(String(64))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ca_revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentPresence(Base):
    """Latest authenticated management address for one active agent node."""

    __tablename__ = "agent_presence"
    __table_args__ = (
        CheckConstraint(
            "length(management_address) BETWEEN 2 AND 45",
            name="ck_agent_presence_management_address_length",
        ),
    )
    node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id", ondelete="CASCADE"), primary_key=True
    )
    certificate_serial: Mapped[str] = mapped_column(
        ForeignKey("agent_certificates.serial"), nullable=False, index=True
    )
    certificate_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    management_address: Mapped[str] = mapped_column(String(45), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class AgentCertificateRotation(Base):
    __tablename__ = "agent_certificate_rotations"
    node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id", ondelete="CASCADE"), primary_key=True
    )
    source_serial: Mapped[str] = mapped_column(String(128), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    csr_pem: Mapped[str] = mapped_column(Text, nullable=False)
    csr_public_key_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_request_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class AgentIssuedCertificateRevocation(Base):
    """Node-independent recovery evidence for a post-issuance CA revocation."""

    __tablename__ = "agent_issued_certificate_revocations"
    serial: Mapped[str] = mapped_column(String(128), primary_key=True)
    node_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    provider_request_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False
    )
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ca_revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentEnrollmentGrant(Base):
    __tablename__ = "agent_enrollment_grants"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('new-node', 're-enroll')",
            name="ck_agent_enrollment_grants_purpose",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    node_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    purpose: Mapped[str] = mapped_column(
        String(24), nullable=False, default="new-node", server_default="new-node"
    )
    token_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentEnrollment(Base):
    __tablename__ = "agent_enrollments"
    __table_args__ = (
        CheckConstraint(
            "state IN ('issuing', 'certificate_issued')",
            name="ck_agent_enrollments_state",
        ),
        CheckConstraint(
            _nullable_lower_hex("observation_receipt_public_key", 64),
            name="ck_agent_enrollments_observation_receipt_public_key",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    grant_id: Mapped[str] = mapped_column(
        ForeignKey("agent_enrollment_grants.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    node_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    csr_pem: Mapped[str] = mapped_column(Text, nullable=False)
    csr_public_key_pem: Mapped[str] = mapped_column(Text, nullable=False)
    csr_public_key_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    host_key_fingerprint: Mapped[str] = mapped_column(String(512), nullable=False)
    hardware_fingerprint: Mapped[str] = mapped_column(String(512), nullable=False)
    agent_digest: Mapped[str] = mapped_column(String(128), nullable=False)
    boot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    observation_receipt_public_key: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    certificate_pem: Mapped[str | None] = mapped_column(Text)
    chain_pem: Mapped[str | None] = mapped_column(Text)
    certificate_serial: Mapped[str | None] = mapped_column(String(128), unique=True)
    certificate_fingerprint: Mapped[str | None] = mapped_column(
        String(128), unique=True
    )
    certificate_generation: Mapped[int | None] = mapped_column(Integer)
    certificate_not_before: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    certificate_not_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


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
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    current_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_disposition: Mapped[str | None] = mapped_column(String(32))
    retry_disposition_attempt: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


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
    progress: Mapped[dict[str, object] | None] = mapped_column(JSON)
    result: Mapped[dict[str, object] | None] = mapped_column(JSON)


class RecipeSourceBundle(Base):
    __tablename__ = "recipe_source_bundles"
    __table_args__ = (
        CheckConstraint(
            _lower_hex("sha256", 64), name="ck_recipe_source_bundle_digest"
        ),
        CheckConstraint(
            "archive_bytes > 0 AND total_bytes >= 0 AND file_count >= 1",
            name="ck_recipe_source_bundle_sizes",
        ),
    )
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    media_type: Mapped[str] = mapped_column(String(96), nullable=False)
    archive_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    manifest: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class SourceBundleArchive(Base):
    """Content-addressed source archive bytes stored in PostgreSQL."""

    __tablename__ = "source_bundle_archives"
    sha256: Mapped[str] = mapped_column(
        ForeignKey("recipe_source_bundles.sha256", ondelete="CASCADE"),
        primary_key=True,
    )
    archive: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


class CatalogEntity(Base):
    __tablename__ = "catalog_entities"
    __table_args__ = (
        UniqueConstraint(
            "kind", "publisher", "slug", name="uq_catalog_entities_identity"
        ),
        CheckConstraint(
            "kind IN ('model-group','model','model-version','execution-harness',"
            "'runtime-distribution','patch-bundle')",
            name="ck_catalog_entities_kind",
        ),
        CheckConstraint(
            "publisher = lower(publisher) AND length(publisher) BETWEEN 2 AND 63",
            name="ck_catalog_entities_publisher",
        ),
        CheckConstraint(
            "slug = lower(slug) AND length(slug) BETWEEN 2 AND 63",
            name="ck_catalog_entities_slug",
        ),
        CheckConstraint(
            "length(title) BETWEEN 1 AND 120", name="ck_catalog_entities_title"
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    publisher: Mapped[str] = mapped_column(String(63), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(63), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class CatalogEntityRevision(Base):
    __tablename__ = "catalog_entity_revisions"
    __table_args__ = (
        UniqueConstraint(
            "entity_id", "revision_number", name="uq_catalog_entity_revision_number"
        ),
        CheckConstraint(
            "revision_number >= 1", name="ck_catalog_entity_revisions_number"
        ),
        CheckConstraint(
            "schema_version = 1", name="ck_catalog_entity_revisions_schema"
        ),
        CheckConstraint(
            "lifecycle IN ('draft','blocked','resolved','deprecated')",
            name="ck_catalog_entity_revisions_lifecycle",
        ),
        CheckConstraint(
            "lifecycle != 'resolved' OR content_sha256 IS NOT NULL",
            name="ck_catalog_entity_revisions_resolved_digest",
        ),
        CheckConstraint(
            _nullable_lower_hex("content_sha256", 64),
            name="ck_catalog_entity_revisions_content_digest",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    entity_id: Mapped[str] = mapped_column(
        ForeignKey("catalog_entities.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    document: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    content_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    entity: Mapped[CatalogEntity] = relationship(lazy="joined")


@event.listens_for(CatalogEntity, "before_delete")
def _catalog_entity_with_revisions_cannot_be_deleted(
    _mapper, connection, target: CatalogEntity
) -> None:
    revision_id = connection.scalar(
        select(CatalogEntityRevision.id)
        .where(CatalogEntityRevision.entity_id == target.id)
        .limit(1)
    )
    if revision_id is not None:
        raise ValueError("catalog entities with revisions cannot be deleted")


@event.listens_for(CatalogEntityRevision, "before_update")
@event.listens_for(CatalogEntityRevision, "before_delete")
def _resolved_catalog_entity_revision_is_immutable(
    _mapper, _connection, target: CatalogEntityRevision
) -> None:
    lifecycle_history = inspect(target).attrs.lifecycle.history
    previous = lifecycle_history.deleted[0] if lifecycle_history.deleted else None
    if target.lifecycle == "resolved" or previous == "resolved":
        raise ValueError("resolved catalog entity revisions are immutable")


@event.listens_for(Session, "before_commit")
def _resolved_catalog_entity_document_is_immutable(session: Session) -> None:
    for value in session.identity_map.values():
        if not isinstance(value, CatalogEntityRevision):
            continue
        if value.lifecycle != "resolved" or value.content_sha256 is None:
            continue
        encoded = json.dumps(
            value.document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != value.content_sha256:
            raise ValueError("resolved catalog entity revisions are immutable")


class LocalRecipe(Base):
    __tablename__ = "local_recipes"
    __table_args__ = (
        CheckConstraint(
            "source_kind IN ('local','workload_run','global','recipe_library')",
            name="ck_local_recipes_source_kind",
        ),
        CheckConstraint(
            "slug = lower(slug) AND length(slug) BETWEEN 2 AND 128",
            name="ck_local_recipes_slug",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    slug: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class LocalRecipeRevision(Base):
    __tablename__ = "local_recipe_revisions"
    __table_args__ = (
        UniqueConstraint(
            "recipe_id", "revision_number", name="uq_local_recipe_revision_number"
        ),
        UniqueConstraint(
            "recipe_id", "content_sha256", name="uq_local_recipe_revision_content"
        ),
        CheckConstraint(
            "revision_number >= 1", name="ck_local_recipe_revisions_number"
        ),
        CheckConstraint("schema_version >= 1", name="ck_local_recipe_revisions_schema"),
        CheckConstraint(
            "lifecycle IN ('draft','blocked','resolved','deprecated')",
            name="ck_local_recipe_revisions_lifecycle",
        ),
        CheckConstraint(
            "lifecycle != 'resolved' OR content_sha256 IS NOT NULL",
            name="ck_local_recipe_revisions_resolved_digest",
        ),
        CheckConstraint(
            _nullable_lower_hex("content_sha256", 64),
            name="ck_local_recipe_revisions_content_digest",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    recipe_id: Mapped[str] = mapped_column(
        ForeignKey("local_recipes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    document: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    content_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


@event.listens_for(LocalRecipeRevision, "before_update")
@event.listens_for(LocalRecipeRevision, "before_delete")
def _resolved_recipe_revision_is_immutable(
    _mapper, _connection, target: LocalRecipeRevision
) -> None:
    if target.lifecycle == "resolved":
        raise ValueError("resolved recipe revisions are immutable")


class RecipeLibrarySyncRun(Base):
    """Durable, idempotent evidence for one managed recipe-library refresh."""

    __tablename__ = "recipe_library_sync_runs"
    __table_args__ = (
        CheckConstraint(
            "trigger IN ('manual','automatic')",
            name="ck_recipe_library_sync_runs_trigger",
        ),
        CheckConstraint(
            "state IN ('running','succeeded','failed')",
            name="ck_recipe_library_sync_runs_state",
        ),
        CheckConstraint(
            _nullable_lower_hex("expected_commit", 40),
            name="ck_recipe_library_sync_runs_expected_commit",
        ),
        CheckConstraint(
            _nullable_lower_hex("observed_commit", 40),
            name="ck_recipe_library_sync_runs_observed_commit",
        ),
        CheckConstraint(
            "total_count >= 0 AND processed_count >= 0 AND imported_count >= 0 "
            "AND updated_count >= 0 AND current_count >= 0 "
            "AND conflict_count >= 0 AND missing_count >= 0 "
            "AND processed_count <= total_count",
            name="ck_recipe_library_sync_runs_counts",
        ),
        CheckConstraint(
            "(state = 'running' AND completed_at IS NULL) OR "
            "(state IN ('succeeded','failed') AND completed_at IS NOT NULL)",
            name="ck_recipe_library_sync_runs_completion",
        ),
        CheckConstraint(
            "(state = 'running' AND active_slot = 'managed-recipes') OR "
            "(state IN ('succeeded','failed') AND active_slot IS NULL)",
            name="ck_recipe_library_sync_runs_active_slot",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    request_key: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    active_slot: Mapped[str | None] = mapped_column(String(32), unique=True)
    repository: Mapped[str] = mapped_column(String(200), nullable=False)
    expected_commit: Mapped[str | None] = mapped_column(String(40))
    observed_commit: Mapped[str | None] = mapped_column(String(40), index=True)
    total_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    imported_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    conflict_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    missing_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_detail: Mapped[str | None] = mapped_column(String(256))
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ManagedRecipeLibraryLink(Base):
    """Stable remote identity bound to the latest imported immutable revision."""

    __tablename__ = "managed_recipe_library_links"
    __table_args__ = (
        UniqueConstraint(
            "repository",
            "publisher",
            "slug",
            name="uq_managed_recipe_library_identity",
        ),
        CheckConstraint(
            "availability IN ('present','missing')",
            name="ck_managed_recipe_library_links_availability",
        ),
        CheckConstraint(
            "sync_state IN ('current','update-available','error')",
            name="ck_managed_recipe_library_links_sync_state",
        ),
        CheckConstraint(
            _lower_hex("remote_content_sha256", 64),
            name="ck_managed_recipe_library_links_digest",
        ),
        CheckConstraint(
            _lower_hex("remote_commit", 40),
            name="ck_managed_recipe_library_links_commit",
        ),
    )
    recipe_id: Mapped[str] = mapped_column(
        ForeignKey("local_recipes.id", ondelete="CASCADE"), primary_key=True
    )
    repository: Mapped[str] = mapped_column(String(200), nullable=False)
    publisher: Mapped[str] = mapped_column(String(63), nullable=False)
    slug: Mapped[str] = mapped_column(String(63), nullable=False)
    source_path: Mapped[str] = mapped_column(String(256), nullable=False)
    remote_commit: Mapped[str] = mapped_column(String(40), nullable=False)
    remote_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    local_revision_id: Mapped[str] = mapped_column(
        ForeignKey("local_recipe_revisions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    availability: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    sync_state: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    last_error: Mapped[str | None] = mapped_column(String(256))
    last_seen_run_id: Mapped[str] = mapped_column(
        ForeignKey("recipe_library_sync_runs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    first_synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class RecipeImport(Base):
    __tablename__ = "recipe_imports"
    __table_args__ = (
        UniqueConstraint(
            "source_kind", "source_sha256", name="uq_recipe_import_source"
        ),
        CheckConstraint(
            "source_kind IN ('local','workload_run','global','recipe_library')",
            name="ck_recipe_imports_source_kind",
        ),
        CheckConstraint(
            _lower_hex("source_sha256", 64), name="ck_recipe_imports_source_digest"
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    recipe_id: Mapped[str] = mapped_column(
        ForeignKey("local_recipes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_reference: Mapped[str] = mapped_column(Text, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    redacted_source: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class RecipeImportItem(Base):
    __tablename__ = "recipe_import_items"
    __table_args__ = (
        CheckConstraint(
            "disposition IN ('imported','incorporated','resolved','transformed','resolution_required',"
            "'overlay_required','unsupported_blocking','dropped_redundant')",
            name="ck_recipe_import_items_disposition",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    import_id: Mapped[str] = mapped_column(
        ForeignKey("recipe_imports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    disposition: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    destination_path: Mapped[str | None] = mapped_column(Text)
    reason_code: Mapped[str] = mapped_column(String(128), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    blocking: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class RecipeGlobalLink(Base):
    __tablename__ = "recipe_global_links"
    __table_args__ = (
        UniqueConstraint(
            "global_publisher", "global_slug", name="uq_recipe_global_link_identity"
        ),
        CheckConstraint("global_revision >= 1", name="ck_recipe_global_links_revision"),
        CheckConstraint(
            _lower_hex("global_content_sha256", 64),
            name="ck_recipe_global_links_digest",
        ),
        CheckConstraint(
            "sync_state IN ('current','local-ahead','remote-ahead','unavailable')",
            name="ck_recipe_global_links_state",
        ),
    )
    recipe_id: Mapped[str] = mapped_column(
        ForeignKey("local_recipes.id", ondelete="CASCADE"), primary_key=True
    )
    global_recipe_id: Mapped[str] = mapped_column(
        String(36), nullable=False, index=True
    )
    global_publisher: Mapped[str] = mapped_column(String(63), nullable=False)
    global_slug: Mapped[str] = mapped_column(String(63), nullable=False)
    global_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    global_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    sync_state: Mapped[str] = mapped_column(String(24), nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RecipeTestReport(Base):
    """Publisher-submitted local test evidence bound to one immutable revision."""

    __tablename__ = "recipe_test_reports"
    __table_args__ = (
        UniqueConstraint(
            "recipe_revision_id", "report_sha256", name="uq_recipe_test_report_digest"
        ),
        CheckConstraint(
            _lower_hex("report_sha256", 64), name="ck_recipe_test_reports_digest"
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    recipe_revision_id: Mapped[str] = mapped_column(
        ForeignKey("local_recipe_revisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    report_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    report: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class RecipeBuild(Base):
    __tablename__ = "recipe_builds"
    __table_args__ = (
        UniqueConstraint(
            "recipe_revision_id",
            "builder_node_id",
            "build_input_sha256",
            name="uq_recipe_build_input_builder",
        ),
        CheckConstraint(
            "state IN ('planned','building','succeeded','failed')",
            name="ck_recipe_builds_state",
        ),
        CheckConstraint(
            _lower_hex("source_bundle_sha256", 64),
            name="ck_recipe_builds_source_digest",
        ),
        CheckConstraint(
            _lower_hex("build_input_sha256", 64),
            name="ck_recipe_builds_input_digest",
        ),
        CheckConstraint(
            "image_digest IS NULL OR "
            "(length(image_digest) = 71 AND substr(image_digest, 1, 7) = 'sha256:')",
            name="ck_recipe_builds_image_digest",
        ),
        CheckConstraint(
            "oci_layout_sha256 IS NULL OR length(oci_layout_sha256) = 64",
            name="ck_recipe_builds_layout_digest",
        ),
        CheckConstraint(
            "image_bytes IS NULL OR image_bytes > 0",
            name="ck_recipe_builds_image_size",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    recipe_revision_id: Mapped[str] = mapped_column(
        ForeignKey("local_recipe_revisions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    builder_node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_bundle_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    build_input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    policy_report: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    plan: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    image_digest: Mapped[str | None] = mapped_column(String(71))
    oci_layout_sha256: Mapped[str | None] = mapped_column(String(64))
    image_bytes: Mapped[int | None] = mapped_column(BigInteger)
    error: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ClusterMapping(Base):
    __tablename__ = "cluster_mappings"
    __table_args__ = (
        CheckConstraint("generation >= 1", name="ck_cluster_mappings_generation"),
        CheckConstraint("node_count >= 1", name="ck_cluster_mappings_node_count"),
        CheckConstraint(
            "state IN ('planned','ready','stale')", name="ck_cluster_mappings_state"
        ),
        CheckConstraint(
            _lower_hex("placement_digest", 64),
            name="ck_cluster_mappings_placement_digest",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    recipe_revision_id: Mapped[str] = mapped_column(
        ForeignKey("local_recipe_revisions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    topology_name: Mapped[str] = mapped_column(String(64), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    node_count: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    parameters: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    placement_digest: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    endpoint_owner_node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id", ondelete="RESTRICT"), nullable=False
    )
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ClusterMappingNode(Base):
    __tablename__ = "cluster_mapping_nodes"
    __table_args__ = (
        UniqueConstraint("mapping_id", "node_id", name="uq_cluster_mapping_node"),
        UniqueConstraint("mapping_id", "rank", name="uq_cluster_mapping_rank"),
        CheckConstraint("rank >= 0", name="ck_cluster_mapping_nodes_rank"),
        CheckConstraint(
            "length(role) BETWEEN 1 AND 64", name="ck_cluster_mapping_nodes_role"
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    mapping_id: Mapped[str] = mapped_column(
        ForeignKey("cluster_mappings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    endpoint_owner: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


def _reject_ready_mapping_node_mutation(
    _mapper: object, connection: object, target: ClusterMappingNode
) -> None:
    state = connection.execute(
        select(ClusterMapping.state).where(ClusterMapping.id == target.mapping_id)
    ).scalar_one_or_none()
    if state == "ready":
        raise ValueError("mapping.ready_immutable")


event.listen(ClusterMappingNode, "before_update", _reject_ready_mapping_node_mutation)
event.listen(ClusterMappingNode, "before_delete", _reject_ready_mapping_node_mutation)


class NodeInventorySnapshot(Base):
    __tablename__ = "node_inventory_snapshots"
    __table_args__ = (
        UniqueConstraint("node_id", "observed_at", name="uq_inventory_node_observed"),
        CheckConstraint(
            "disk_total_bytes>=0 AND disk_free_bytes>=0 AND disk_free_bytes<=disk_total_bytes",
            name="ck_inventory_disk",
        ),
        CheckConstraint(
            "host_memory_total_bytes>=0 AND host_memory_free_bytes>=0 AND host_memory_free_bytes<=host_memory_total_bytes",
            name="ck_inventory_host_memory",
        ),
        CheckConstraint(
            "gpu_memory_total_bytes>=0 AND gpu_memory_free_bytes>=0 AND gpu_memory_free_bytes<=gpu_memory_total_bytes AND gpu_count>=0",
            name="ck_inventory_gpu_memory",
        ),
        CheckConstraint(
            "(fabric_address IS NULL AND fabric_bandwidth_mbps IS NULL) OR (fabric_address IS NOT NULL AND fabric_bandwidth_mbps>0)",
            name="ck_inventory_fabric",
        ),
        CheckConstraint(_lower_hex("evidence_digest", 64), name="ck_inventory_digest"),
        Index("ix_inventory_node_observed", "node_id", "observed_at"),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id", ondelete="CASCADE"), nullable=False
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    disk_total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    disk_free_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    host_memory_total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    host_memory_free_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gpu_memory_total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gpu_memory_free_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gpu_count: Mapped[int] = mapped_column(Integer, nullable=False)
    fabric_address: Mapped[str | None] = mapped_column(String(45))
    fabric_bandwidth_mbps: Mapped[int | None] = mapped_column(BigInteger)
    nvidia_driver_version: Mapped[str] = mapped_column(
        String(256), nullable=False, default="unknown", server_default="unknown"
    )
    container_runtime_version: Mapped[str] = mapped_column(
        String(256), nullable=False, default="unknown", server_default="unknown"
    )
    artifact_store_read_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    evidence_digest: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )


class NodeTelemetrySample(Base):
    __tablename__ = "node_telemetry_samples"
    __table_args__ = (
        UniqueConstraint(
            "node_id", "boot_id", "sequence", name="uq_telemetry_node_boot_sequence"
        ),
        UniqueConstraint("node_id", "id", name="uq_telemetry_node_sample"),
        CheckConstraint(_uuid_shape("boot_id"), name="ck_telemetry_boot_id_shape"),
        CheckConstraint(
            "sequence BETWEEN 0 AND 9223372036854775807 AND "
            "gap_samples BETWEEN 0 AND 9223372036854775807",
            name="ck_telemetry_sequences",
        ),
        CheckConstraint(
            "(cpu_utilization_percent IS NULL OR "
            "cpu_utilization_percent BETWEEN 0 AND 100) AND "
            "(gpu_utilization_percent IS NULL OR "
            "gpu_utilization_percent BETWEEN 0 AND 100)",
            name="ck_telemetry_utilization",
        ),
        CheckConstraint(
            "load_average_1m IS NULL OR load_average_1m BETWEEN 0 AND 1000000",
            name="ck_telemetry_load",
        ),
        CheckConstraint(
            "(memory_total_bytes IS NULL AND memory_available_bytes IS NULL) OR "
            "(memory_total_bytes IS NOT NULL AND memory_available_bytes IS NOT NULL AND "
            "memory_total_bytes >= 0 AND memory_available_bytes >= 0 AND "
            "memory_total_bytes <= 17592186044416 AND "
            "memory_available_bytes <= 17592186044416 AND "
            "memory_available_bytes <= memory_total_bytes)",
            name="ck_telemetry_memory",
        ),
        CheckConstraint(
            "(disk_total_bytes IS NULL AND disk_free_bytes IS NULL) OR "
            "(disk_total_bytes IS NOT NULL AND disk_free_bytes IS NOT NULL AND "
            "disk_total_bytes >= 0 AND disk_free_bytes >= 0 AND "
            "disk_total_bytes <= 17592186044416 AND "
            "disk_free_bytes <= 17592186044416 AND "
            "disk_free_bytes <= disk_total_bytes)",
            name="ck_telemetry_disk",
        ),
        CheckConstraint(
            "(gpu_memory_total_bytes IS NULL AND gpu_memory_free_bytes IS NULL) OR "
            "(gpu_memory_total_bytes IS NOT NULL AND gpu_memory_free_bytes IS NOT NULL AND "
            "gpu_memory_total_bytes >= 0 AND gpu_memory_free_bytes >= 0 AND "
            "gpu_memory_total_bytes <= 17592186044416 AND "
            "gpu_memory_free_bytes <= 17592186044416 AND "
            "gpu_memory_free_bytes <= gpu_memory_total_bytes)",
            name="ck_telemetry_gpu_memory",
        ),
        CheckConstraint(
            "(temperature_c IS NULL OR temperature_c BETWEEN -100 AND 300) "
            "AND (power_watts IS NULL OR power_watts BETWEEN 0 AND 100000) AND "
            "(network_receive_bytes_per_second IS NULL OR "
            "network_receive_bytes_per_second BETWEEN 0 AND 1000000000000000) AND "
            "(network_transmit_bytes_per_second IS NULL OR "
            "network_transmit_bytes_per_second BETWEEN 0 AND 1000000000000000)",
            name="ck_telemetry_physical_metrics",
        ),
        CheckConstraint(
            "length(CAST(details AS TEXT)) BETWEEN 2 AND 4096",
            name="ck_telemetry_details",
        ),
        Index("ix_telemetry_node_observed", "node_id", "observed_at"),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id", ondelete="CASCADE"), nullable=False
    )
    boot_id: Mapped[str] = mapped_column(String(36), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    cpu_utilization_percent: Mapped[float | None] = mapped_column(Float)
    load_average_1m: Mapped[float | None] = mapped_column(Float)
    memory_total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    memory_available_bytes: Mapped[int | None] = mapped_column(BigInteger)
    disk_total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    disk_free_bytes: Mapped[int | None] = mapped_column(BigInteger)
    gpu_utilization_percent: Mapped[float | None] = mapped_column(Float)
    gpu_memory_total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    gpu_memory_free_bytes: Mapped[int | None] = mapped_column(BigInteger)
    temperature_c: Mapped[float | None] = mapped_column(Float)
    power_watts: Mapped[float | None] = mapped_column(Float)
    network_receive_bytes_per_second: Mapped[float | None] = mapped_column(Float)
    network_transmit_bytes_per_second: Mapped[float | None] = mapped_column(Float)
    gap_samples: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    details: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    # Rich schema-2 observations live separately from the historical scalar
    # columns.  Keeping the old columns makes rollups and old evidence stable;
    # this bounded JSON document carries per-device, per-interface and
    # per-run series plus capability/provenance metadata.
    metrics: Mapped[dict[str, object]] = mapped_column(
        JSON, nullable=False, default=dict, server_default="{}"
    )


class NodeTelemetryLatest(Base):
    __tablename__ = "node_telemetry_latest"
    __table_args__ = (
        ForeignKeyConstraint(
            ("node_id", "sample_id"),
            ("node_telemetry_samples.node_id", "node_telemetry_samples.id"),
            name="fk_telemetry_latest_node_sample",
            ondelete="RESTRICT",
        ),
    )
    node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id", ondelete="CASCADE"), primary_key=True
    )
    sample_id: Mapped[str] = mapped_column(
        nullable=False,
        unique=True,
    )


class NodeTelemetryRollupBucket(Base):
    __tablename__ = "node_telemetry_rollup_buckets"
    __table_args__ = (
        CheckConstraint(
            "resolution_seconds IN (60, 900)",
            name="ck_telemetry_rollup_buckets_resolution",
        ),
        CheckConstraint(
            "source_sample_count BETWEEN 0 AND 9223372036854775807 AND "
            "gap_samples BETWEEN 0 AND 9223372036854775807",
            name="ck_telemetry_rollup_buckets_counts",
        ),
        Index(
            "ix_telemetry_rollup_buckets_resolution_start",
            "resolution_seconds",
            "bucket_start",
            "node_id",
        ),
    )
    resolution_seconds: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    node_id: Mapped[str] = mapped_column(
        ForeignKey(
            "agent_nodes.node_id",
            name="fk_telemetry_rollup_buckets_node",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )
    bucket_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    source_sample_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gap_samples: Mapped[int] = mapped_column(BigInteger, nullable=False)


class NodeTelemetryRollupMetric(Base):
    __tablename__ = "node_telemetry_rollup_metrics"
    __table_args__ = (
        ForeignKeyConstraint(
            ("resolution_seconds", "node_id", "bucket_start"),
            (
                "node_telemetry_rollup_buckets.resolution_seconds",
                "node_telemetry_rollup_buckets.node_id",
                "node_telemetry_rollup_buckets.bucket_start",
            ),
            name="fk_telemetry_rollup_metrics_bucket",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "resolution_seconds IN (60, 900)",
            name="ck_telemetry_rollup_metrics_resolution",
        ),
        CheckConstraint(
            "length(metric_name) BETWEEN 1 AND 64",
            name="ck_telemetry_rollup_metrics_name",
        ),
        CheckConstraint(
            "key IS NULL OR length(key) BETWEEN 1 AND 96",
            name="ck_telemetry_rollup_metrics_key",
        ),
        CheckConstraint(
            "scope IS NULL OR length(scope) BETWEEN 1 AND 16",
            name="ck_telemetry_rollup_metrics_scope",
        ),
        CheckConstraint(
            "device_id IS NULL OR length(device_id) BETWEEN 1 AND 128",
            name="ck_telemetry_rollup_metrics_device",
        ),
        CheckConstraint(
            "process_id IS NULL OR process_id BETWEEN 1 AND 2147483647",
            name="ck_telemetry_rollup_metrics_process",
        ),
        CheckConstraint(
            "process_name IS NULL OR length(process_name) BETWEEN 1 AND 128",
            name="ck_telemetry_rollup_metrics_process_name",
        ),
        CheckConstraint(
            "interface_name IS NULL OR length(interface_name) BETWEEN 1 AND 64",
            name="ck_telemetry_rollup_metrics_interface",
        ),
        CheckConstraint(
            "run_id IS NULL OR length(run_id) BETWEEN 1 AND 128",
            name="ck_telemetry_rollup_metrics_run",
        ),
        CheckConstraint(
            "length(unit) BETWEEN 1 AND 32 AND length(source) BETWEEN 1 AND 128 AND "
            "length(measurement_kind) BETWEEN 1 AND 16 AND length(aggregation) BETWEEN 1 AND 32",
            name="ck_telemetry_rollup_metrics_metadata",
        ),
        CheckConstraint(
            "sample_count BETWEEN 0 AND 9223372036854775807",
            name="ck_telemetry_rollup_metrics_count",
        ),
        CheckConstraint(
            "minimum BETWEEN -1e308 AND 1e308 AND "
            "mean BETWEEN -1e308 AND 1e308 AND "
            "maximum BETWEEN -1e308 AND 1e308 AND "
            "minimum <= mean AND mean <= maximum",
            name="ck_telemetry_rollup_metrics_values",
        ),
    )
    resolution_seconds: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    node_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    bucket_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    metric_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Rich identity and provenance are retained with each rollup row.  The
    # bounded metric_name is only the stable storage key; consumers must use
    # these columns to distinguish devices, processes, interfaces and runs.
    key: Mapped[str | None] = mapped_column(String(96))
    scope: Mapped[str | None] = mapped_column(String(16))
    device_id: Mapped[str | None] = mapped_column(String(128))
    process_id: Mapped[int | None] = mapped_column(BigInteger)
    process_name: Mapped[str | None] = mapped_column(String(128))
    interface_name: Mapped[str | None] = mapped_column(String(64))
    run_id: Mapped[str | None] = mapped_column(String(128))
    unit: Mapped[str] = mapped_column(
        String(32), nullable=False, default="unknown", server_default="unknown"
    )
    source: Mapped[str] = mapped_column(
        String(128), nullable=False, default="legacy", server_default="legacy"
    )
    measurement_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="measured", server_default="measured"
    )
    aggregation: Mapped[str] = mapped_column(
        String(32), nullable=False, default="mean", server_default="mean"
    )
    sample_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    minimum: Mapped[float] = mapped_column(Float, nullable=False)
    mean: Mapped[float] = mapped_column(Float, nullable=False)
    maximum: Mapped[float] = mapped_column(Float, nullable=False)


class NodeTelemetryRollupDirty(Base):
    __tablename__ = "node_telemetry_rollup_dirty"
    __table_args__ = (
        CheckConstraint(
            "resolution_seconds IN (60, 900)",
            name="ck_telemetry_rollup_dirty_resolution",
        ),
        Index(
            "ix_telemetry_rollup_dirty_resolution_start",
            "resolution_seconds",
            "bucket_start",
            "node_id",
        ),
    )
    resolution_seconds: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    node_id: Mapped[str] = mapped_column(
        ForeignKey(
            "agent_nodes.node_id",
            name="fk_telemetry_rollup_dirty_node",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )
    bucket_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )


class TelemetryMaintenanceState(Base):
    """Durable singleton used to coordinate bounded rollup fairness."""

    __tablename__ = "telemetry_maintenance_state"
    __table_args__ = (
        CheckConstraint(
            "singleton_id = 1", name="ck_telemetry_maintenance_state_singleton"
        ),
        CheckConstraint(
            "next_resolution_seconds IN (60, 900)",
            name="ck_telemetry_maintenance_state_resolution",
        ),
    )
    singleton_id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    next_resolution_seconds: Mapped[int] = mapped_column(SmallInteger, nullable=False)


@event.listens_for(TelemetryMaintenanceState.__table__, "after_create")
def _seed_telemetry_maintenance_state(_target, connection, **_kw) -> None:
    connection.execute(
        TelemetryMaintenanceState.__table__.insert().values(
            singleton_id=1,
            next_resolution_seconds=60,
        )
    )


class FleetEventCursor(Base):
    __tablename__ = "fleet_event_cursor"
    __table_args__ = (
        CheckConstraint("singleton_id = 1", name="ck_fleet_event_cursor_singleton"),
        CheckConstraint("last_id >= 0", name="ck_fleet_event_cursor_last_id"),
    )
    singleton_id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    last_id: Mapped[int] = mapped_column(BigInteger, nullable=False)


@event.listens_for(FleetEventCursor.__table__, "after_create")
def _seed_fleet_event_cursor(_target, connection, **_kw) -> None:
    connection.execute(
        FleetEventCursor.__table__.insert().values(singleton_id=1, last_id=0)
    )


class FleetStreamEvent(Base):
    __tablename__ = "fleet_stream_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('node-telemetry','node-profile','recipe-state','operation-state')",
            name="ck_fleet_stream_events_event_type",
        ),
        CheckConstraint(
            "expires_at > occurred_at", name="ck_fleet_stream_events_expiry"
        ),
        CheckConstraint(
            _Utf8ByteLength(literal_column("payload")).between(2, 8192),
            name="ck_fleet_stream_events_payload_size",
        ),
        Index("ix_fleet_stream_events_expires_id", "expires_at", "id"),
        Index("ix_fleet_stream_events_node_id", "node_id", "id"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    node_id: Mapped[str | None] = mapped_column(String(36))
    entity_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class NodeArtifact(Base):
    __tablename__ = "node_artifacts"
    __table_args__ = (
        UniqueConstraint("node_id", "digest", name="uq_node_artifact_digest"),
        CheckConstraint(
            "kind IN ('image','image-layer','model','auxiliary')",
            name="ck_node_artifacts_kind",
        ),
        CheckConstraint(
            "state IN ('partial','verified','missing','corrupt')",
            name="ck_node_artifacts_state",
        ),
        CheckConstraint(
            "size_bytes>=0 AND ref_count>=0", name="ck_node_artifacts_sizes"
        ),
        CheckConstraint(_lower_hex("digest", 64), name="ck_node_artifacts_digest"),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    ref_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class RecipeInstallation(Base):
    __tablename__ = "recipe_installations"
    __table_args__ = (
        CheckConstraint(
            _lower_hex("plan_digest", 64), name="ck_recipe_installations_digest"
        ),
        CheckConstraint(
            "state IN ('planned','installing','installed','partial','failed','uninstalled')",
            name="ck_recipe_installations_state",
        ),
        CheckConstraint(
            _nullable_lower_hex("model_version_sha256", 64),
            name="ck_recipe_installations_model_version_sha256",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    recipe_revision_id: Mapped[str] = mapped_column(
        ForeignKey("local_recipe_revisions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # Persist the exact primary model identity accepted with the immutable
    # recipe revision.  This makes model-to-installation ownership explicit
    # without attempting to infer it from mutable catalog display metadata.
    model_version_sha256: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    mapping_id: Mapped[str] = mapped_column(
        ForeignKey("cluster_mappings.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    mapping_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    recipe_build_id: Mapped[str] = mapped_column(
        ForeignKey("recipe_builds.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    image_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    # A digest identifies the approved plan contents, not one installation row.
    # Reinstalling the same immutable plan after uninstall is legitimate and must
    # not depend on transient inventory noise to manufacture a new digest.
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    plan: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class InstallationNode(Base):
    __tablename__ = "installation_nodes"
    __table_args__ = (
        UniqueConstraint("installation_id", "node_id", name="uq_installation_node"),
        CheckConstraint(
            "required_bytes>=0 AND installed_bytes>=0",
            name="ck_installation_nodes_bytes",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    installation_id: Mapped[str] = mapped_column(
        ForeignKey("recipe_installations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    required_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    installed_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    evidence_digest: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class RecipeRun(Base):
    __tablename__ = "recipe_runs"
    __table_args__ = (
        CheckConstraint(_lower_hex("plan_digest", 64), name="ck_recipe_runs_digest"),
        CheckConstraint(
            "state IN ('planned','starting','running','stopping','stopped','failed','lost')",
            name="ck_recipe_runs_state",
        ),
        CheckConstraint(
            "route_state IN ('withdrawn','pending','published','failed')",
            name="ck_recipe_runs_route_state",
        ),
        CheckConstraint(
            "route_generation IS NULL OR route_generation>=1",
            name="ck_recipe_runs_route_generation",
        ),
        CheckConstraint(
            "run_generation>=1",
            name="ck_recipe_runs_run_generation",
        ),
        CheckConstraint(
            "route_digest IS NULL OR length(route_digest)=64",
            name="ck_recipe_runs_route_digest",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    installation_id: Mapped[str] = mapped_column(
        ForeignKey("recipe_installations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    mapping_id: Mapped[str] = mapped_column(
        ForeignKey("cluster_mappings.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    mapping_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    run_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1, server_default="1"
    )
    observation_deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    alias: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    plan: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    route_state: Mapped[str] = mapped_column(
        String(24), nullable=False, default="withdrawn", server_default="withdrawn"
    )
    route_generation: Mapped[int | None] = mapped_column(BigInteger)
    route_digest: Mapped[str | None] = mapped_column(String(64))
    route_error: Mapped[str | None] = mapped_column(String(512))
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunNode(Base):
    __tablename__ = "run_nodes"
    __table_args__ = (
        UniqueConstraint("run_id", "node_id", name="uq_run_node"),
        UniqueConstraint("run_id", "rank", name="uq_run_rank"),
        CheckConstraint(
            "rank>=0 AND port BETWEEN 1024 AND 65535 AND reserved_memory_bytes>=0 AND (observed_memory_bytes IS NULL OR observed_memory_bytes>=0)",
            name="ck_run_nodes_resources",
        ),
        CheckConstraint("length(role) BETWEEN 1 AND 64", name="ck_run_nodes_role"),
        CheckConstraint(
            "observed_run_generation IS NULL OR observed_run_generation>=1",
            name="ck_run_nodes_observed_run_generation",
        ),
        CheckConstraint(
            _nullable_lower_hex("observation_receipt_sha256", 64),
            name="ck_run_nodes_observation_receipt",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("recipe_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_memory_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    observed_memory_bytes: Mapped[int | None] = mapped_column(BigInteger)
    endpoint: Mapped[dict[str, object] | None] = mapped_column(JSON)
    evidence_digest: Mapped[str | None] = mapped_column(String(64))
    observed_run_generation: Mapped[int | None] = mapped_column(BigInteger)
    observation_receipt_sha256: Mapped[str | None] = mapped_column(String(64))
    observation_endpoint_ready: Mapped[bool | None] = mapped_column(Boolean)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class RecipeRunObservationGrant(Base):
    """Current exact-inspection grant nonce for one retained local rank."""

    __tablename__ = "recipe_run_observation_grants"
    __table_args__ = (
        CheckConstraint(
            _lower_hex("identity_sha256", 64),
            name="ck_recipe_run_observation_grants_identity",
        ),
        CheckConstraint(
            "expires_at >= issued_at",
            name="ck_recipe_run_observation_grants_expiry",
        ),
    )
    run_node_id: Mapped[str] = mapped_column(
        ForeignKey("run_nodes.id", ondelete="CASCADE"), primary_key=True
    )
    request_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    identity_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    issued_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    consumed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )


class ArtifactJobBlob(Base):
    """Immutable content-addressed bytes staged for or returned by a recipe job."""

    __tablename__ = "artifact_job_blobs"
    __table_args__ = (
        CheckConstraint(_lower_hex("sha256", 64), name="ck_artifact_job_blobs_digest"),
        CheckConstraint("size_bytes >= 0", name="ck_artifact_job_blobs_size"),
    )
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ArtifactJob(Base):
    """Persisted controller authority for one artifact-producing run invocation."""

    __tablename__ = "artifact_jobs"
    __table_args__ = (
        CheckConstraint(
            "interface IN ('audio-job','video-job','image-job','mesh-job','artifact-job')",
            name="ck_artifact_jobs_interface",
        ),
        CheckConstraint(
            "state IN ('draft','ready','queued','running','cancelling','waiting-for-operator','succeeded','failed','cancelled')",
            name="ck_artifact_jobs_state",
        ),
        CheckConstraint(
            "input_total_bytes >= 0 AND timeout_seconds BETWEEN 1 AND 3600",
            name="ck_artifact_jobs_limits",
        ),
        CheckConstraint(
            _lower_hex("input_manifest_sha256", 64),
            name="ck_artifact_jobs_input_manifest",
        ),
        CheckConstraint(
            _lower_hex("contract_sha256", 64),
            name="ck_artifact_jobs_contract",
        ),
        CheckConstraint(
            _nullable_lower_hex("output_manifest_sha256", 64),
            name="ck_artifact_jobs_output_manifest",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("recipe_runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="RESTRICT"), unique=True, index=True
    )
    request_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    interface: Mapped[str] = mapped_column(String(24), nullable=False)
    parameters: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    output_limits: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    compiled_contract: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    contract_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    input_manifest: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    input_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    input_total_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    output_manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    output_manifest: Mapped[dict[str, object] | None] = mapped_column(JSON)
    result_evidence: Mapped[dict[str, object] | None] = mapped_column(JSON)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    status_reason: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ArtifactJobFile(Base):
    __tablename__ = "artifact_job_files"
    __table_args__ = (
        UniqueConstraint(
            "artifact_job_id", "direction", "name", name="uq_artifact_job_file_name"
        ),
        CheckConstraint(
            "direction IN ('input','output')", name="ck_artifact_job_files_direction"
        ),
        CheckConstraint("size_bytes >= 0", name="ck_artifact_job_files_size"),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    artifact_job_id: Mapped[str] = mapped_column(
        ForeignKey("artifact_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    slot: Mapped[str | None] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    media_type: Mapped[str] = mapped_column(String(129), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    blob_sha256: Mapped[str] = mapped_column(
        ForeignKey("artifact_job_blobs.sha256", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ResourceReservation(Base):
    __tablename__ = "resource_reservations"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('disk','unified-memory','host-memory','gpu-memory','port')",
            name="ck_reservations_kind",
        ),
        CheckConstraint(
            "state IN ('active','released','expired') AND amount_bytes>=0",
            name="ck_reservations_state",
        ),
        CheckConstraint(_lower_hex("plan_digest", 64), name="ck_reservations_digest"),
        Index("ix_reservations_node_state", "node_id", "state"),
        Index(
            "uq_active_node_port",
            "node_id",
            "kind",
            "resource_key",
            unique=True,
            postgresql_where=text("state='active' AND kind='port'"),
            sqlite_where=text("state='active' AND kind='port'"),
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    resource_key: Mapped[str] = mapped_column(String(128), nullable=False)
    amount_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    owner_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(36), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Recipe(Base):
    """Greenfield Library recipe identity owned by PostgreSQL."""

    __tablename__ = "recipes"
    recipe_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class RecipeRevision(Base):
    """Content-addressed immutable revision in the greenfield Library."""

    __tablename__ = "recipe_revisions"
    __table_args__ = (
        UniqueConstraint(
            "recipe_id", "revision_number", name="uq_recipe_revision_number"
        ),
        UniqueConstraint(
            "recipe_id", "content_digest", name="uq_recipe_revision_digest"
        ),
        CheckConstraint("revision_number >= 1", name="ck_recipe_revision_number"),
    )
    revision_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    recipe_id: Mapped[str] = mapped_column(
        ForeignKey("recipes.recipe_id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


@event.listens_for(RecipeRevision, "before_update")
@event.listens_for(RecipeRevision, "before_delete")
def _recipe_revision_is_immutable(_mapper, _connection, target: RecipeRevision) -> None:
    raise ValueError("recipe revisions are immutable")
