"""Local catalog authority and operational database state.

PostgreSQL is authoritative for recipes, revisions, placement, and runtime
state. Git remains an immutable source for legacy package definitions and
signed release evidence while those adapters are migrated.
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
    base_commit: Mapped[str] = mapped_column(String(128), nullable=False)
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


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    request_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    base_commit: Mapped[str | None] = mapped_column(String(128))
    targets: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
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
    """A completed scheduler loop bound to one immutable control generation."""

    __tablename__ = "control_process_heartbeats"
    __table_args__ = (
        UniqueConstraint(
            "process_kind",
            "start_nonce",
            name="uq_control_process_heartbeats_process_start",
        ),
        CheckConstraint(
            "process_kind = 'worker'",
            name="ck_control_process_heartbeats_process_kind",
        ),
        CheckConstraint(
            "length(generation_id) BETWEEN 1 AND 128",
            name="ck_control_process_heartbeats_generation_id_length",
        ),
        CheckConstraint(
            "length(release_digest) = 71 AND "
            "substr(release_digest, 1, 7) = 'sha256:' AND "
            f"({_lower_hex('substr(release_digest, 8, 64)', 64)})",
            name="ck_control_process_heartbeats_release_digest",
        ),
        CheckConstraint(
            "length(build_digest) = 71 AND "
            "substr(build_digest, 1, 7) = 'sha256:' AND "
            f"({_lower_hex('substr(build_digest, 8, 64)', 64)})",
            name="ck_control_process_heartbeats_build_digest",
        ),
        CheckConstraint(
            _lower_hex("start_nonce", 64),
            name="ck_control_process_heartbeats_start_nonce",
        ),
        CheckConstraint(
            "loop_sequence >= 1",
            name="ck_control_process_heartbeats_loop_sequence",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    process_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    generation_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    release_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    build_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    start_nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    loop_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class Reconciliation(Base):
    __tablename__ = "reconciliations"
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    base_commit: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    graph: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        default=lambda: {
            "base_commit": "",
            "nodes": [],
            "schema_version": 1,
            "targets": [],
        },
        server_default='{"base_commit":"","nodes":[],"schema_version":1,"targets":[]}',
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
        String(32), nullable=False, default="legacy", server_default="legacy"
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


class AgentNode(Base):
    __tablename__ = "agent_nodes"
    __table_args__ = (
        CheckConstraint(
            "architecture IS NULL OR architecture IN ('linux-arm64', 'linux-x86_64')",
            name="ck_agent_nodes_architecture",
        ),
        CheckConstraint(
            "agent_implementation IN ('pending', 'python', 'rust')",
            name="ck_agent_nodes_implementation",
        ),
        CheckConstraint(
            "migration_state IN ('required', 'complete')",
            name="ck_agent_nodes_migration_state",
        ),
        CheckConstraint(
            "(agent_implementation = 'rust' AND migration_state = 'complete') OR "
            "(agent_implementation IN ('pending', 'python') AND migration_state = 'required')",
            name="ck_agent_nodes_migration_consistency",
        ),
    )
    node_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    agent_implementation: Mapped[str] = mapped_column(
        String(16), nullable=False, default="python", server_default="pending"
    )
    migration_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="required", server_default="required"
    )
    protocol_version: Mapped[int | None] = mapped_column(Integer)
    architecture: Mapped[str | None] = mapped_column(String(16))
    platform_version: Mapped[str | None] = mapped_column(String(32))
    build_digest: Mapped[str | None] = mapped_column(String(71))
    active_slot: Mapped[str | None] = mapped_column(String(1))
    agent_sha256: Mapped[str | None] = mapped_column(String(64))
    supervisor_generation: Mapped[int | None] = mapped_column(Integer)
    supervisor_ready_generation: Mapped[int | None] = mapped_column(Integer)
    self_test_passed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    contact_certificate_serial: Mapped[str | None] = mapped_column(String(128))
    contact_observation_digest: Mapped[str | None] = mapped_column(String(64))
    capabilities: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NodeMutationLease(Base):
    """Exclusive durable ownership of one node's mutations and route state."""

    __tablename__ = "node_mutation_leases"
    __table_args__ = (
        CheckConstraint(
            "owner_kind IN ('update-rollout', 'reconciliation')",
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
            "purpose IN ('new-node', 'rust-migration')",
            name="ck_agent_enrollment_grants_purpose",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    node_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    decision_actor: Mapped[str | None] = mapped_column(String(200))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
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
    base_commit: Mapped[str] = mapped_column(String(128), nullable=False)
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


class UpdateRollout(Base):
    """Immutable platform-update plan and durable orchestration cursor."""

    __tablename__ = "update_rollouts"
    __table_args__ = (
        CheckConstraint(
            "state IN ('planned', 'withdrawing', 'updating', 'soaking', "
            "'publishing', 'failure-publishing', 'compensating-withdrawal', "
            "'paused', 'rolling-back', "
            "'rollback-publishing', 'waiting-for-approval', 'completed', 'partial', 'failed')",
            name="ck_update_rollouts_state",
        ),
        CheckConstraint(
            _lower_hex("plan_digest", 64),
            name="ck_update_rollouts_plan_digest_length",
        ),
        CheckConstraint(
            _lower_hex("release_digest", 64),
            name="ck_update_rollouts_release_digest_length",
        ),
        CheckConstraint(
            _lower_hex("fleet_digest", 64),
            name="ck_update_rollouts_fleet_digest_length",
        ),
        CheckConstraint(
            _lower_hex("topology_digest", 64),
            name="ck_update_rollouts_topology_digest_length",
        ),
        CheckConstraint(
            _lower_hex("agent_input_digest", 64),
            name="ck_update_rollouts_agent_input_digest_length",
        ),
        CheckConstraint(
            "length(target_build_digest) = 71 AND "
            "substr(target_build_digest, 1, 7) = 'sha256:' AND "
            f"({_lower_hex('substr(target_build_digest, 8, 64)', 64)})",
            name="ck_update_rollouts_target_build_digest",
        ),
        CheckConstraint(
            "current_batch >= 0",
            name="ck_update_rollouts_current_batch",
        ),
        CheckConstraint(
            "tuf_targets_version >= 1",
            name="ck_update_rollouts_tuf_targets_version",
        ),
        CheckConstraint(
            _nullable_lower_hex("failure_evidence_digest", 64),
            name="ck_update_rollouts_failure_evidence_digest_length",
        ),
        CheckConstraint(
            _nullable_lower_hex("rollback_evidence_digest", 64),
            name="ck_update_rollouts_rollback_evidence_digest_length",
        ),
        CheckConstraint(
            _nullable_lower_hex("approval_evidence_digest", 64),
            name="ck_update_rollouts_approval_evidence_digest_length",
        ),
        CheckConstraint(
            "(state IN ('completed', 'partial') AND completed_at IS NOT NULL) OR "
            "(state NOT IN ('completed', 'partial') AND completed_at IS NULL)",
            name="ck_update_rollouts_completion_state",
        ),
        CheckConstraint(
            "(approval_at IS NULL AND approval_actor IS NULL AND "
            "approval_request_id IS NULL AND approval_reason IS NULL AND "
            "approval_evidence_digest IS NULL) OR "
            "(approval_at IS NOT NULL AND approval_actor IS NOT NULL AND "
            "approval_request_id IS NOT NULL AND approval_reason IS NOT NULL AND "
            "approval_evidence_digest IS NOT NULL)",
            name="ck_update_rollouts_approval_complete",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), unique=True
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    release_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    base_commit: Mapped[str] = mapped_column(String(128), nullable=False)
    fleet_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    topology_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    target_platform_version: Mapped[str] = mapped_column(String(32), nullable=False)
    target_build_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    tuf_targets_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )
    update_admin_grant: Mapped[dict[str, object] | None] = mapped_column(JSON)
    rollback_admin_grant: Mapped[dict[str, object] | None] = mapped_column(JSON)
    plan: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    current_batch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    soak_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    failure_evidence_digest: Mapped[str | None] = mapped_column(String(64))
    rollback_evidence_digest: Mapped[str | None] = mapped_column(String(64))
    approval_actor: Mapped[str | None] = mapped_column(String(200))
    approval_request_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    approval_reason: Mapped[str | None] = mapped_column(Text)
    approval_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approval_evidence_digest: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UpdateRolloutNode(Base):
    """Per-node update progress, operation bindings, and acceptance evidence."""

    __tablename__ = "update_rollout_nodes"
    __table_args__ = (
        UniqueConstraint(
            "rollout_id",
            "node_id",
            name="uq_update_rollout_nodes_rollout_node",
        ),
        UniqueConstraint(
            "rollout_id",
            "batch_index",
            "node_order",
            name="uq_update_rollout_nodes_batch_order",
        ),
        CheckConstraint(
            "state IN ('offline-pending', 'pending', 'routes-withdrawn', 'updating', 'soaking', "
            "'accepted', 'failed', 'rolling-back', 'rolled-back')",
            name="ck_update_rollout_nodes_state",
        ),
        CheckConstraint(
            "batch_index >= -1 AND node_order >= 0",
            name="ck_update_rollout_nodes_order",
        ),
        CheckConstraint(
            _lower_hex("source_identity_digest", 64),
            name="ck_update_rollout_nodes_source_identity_digest_length",
        ),
        CheckConstraint(
            _lower_hex("target_artifact_digest", 64),
            name="ck_update_rollout_nodes_target_artifact_digest_length",
        ),
        CheckConstraint(
            "observed_build_digest IS NULL OR "
            "(length(observed_build_digest) = 71 AND "
            "substr(observed_build_digest, 1, 7) = 'sha256:' AND "
            f"({_lower_hex('substr(observed_build_digest, 8, 64)', 64)}))",
            name="ck_update_rollout_nodes_observed_build_digest",
        ),
        CheckConstraint(
            "observed_active_slot IS NULL OR observed_active_slot IN ('A', 'B')",
            name="ck_update_rollout_nodes_observed_active_slot",
        ),
        CheckConstraint(
            _nullable_lower_hex("route_withdrawal_evidence_digest", 64),
            name="ck_update_rollout_nodes_route_evidence_digest_length",
        ),
        CheckConstraint(
            _nullable_lower_hex("acceptance_evidence_digest", 64),
            name="ck_update_rollout_nodes_acceptance_evidence_digest_length",
        ),
        CheckConstraint(
            _nullable_lower_hex("failure_evidence_digest", 64),
            name="ck_update_rollout_nodes_failure_evidence_digest_length",
        ),
        CheckConstraint(
            _nullable_lower_hex("rollback_evidence_digest", 64),
            name="ck_update_rollout_nodes_rollback_evidence_digest_length",
        ),
        CheckConstraint(
            "(state IN ('offline-pending', 'pending', 'routes-withdrawn') AND dispatch_at IS NULL "
            "AND activation_deadline IS NULL) OR "
            "(state IN ('updating', 'soaking', 'accepted', 'failed', "
            "'rolling-back', 'rolled-back') AND dispatch_at IS NOT NULL AND "
            "activation_deadline IS NOT NULL AND activation_deadline > dispatch_at)",
            name="ck_update_rollout_nodes_dispatch_window",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    rollout_id: Mapped[str] = mapped_column(
        ForeignKey("update_rollouts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id"), nullable=False, index=True
    )
    batch_index: Mapped[int] = mapped_column(Integer, nullable=False)
    node_order: Mapped[int] = mapped_column(Integer, nullable=False)
    is_canary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_operations.id"), unique=True
    )
    rollback_operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_operations.id"), unique=True
    )
    operation_history: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    source_identity_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    target_artifact_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_platform_version: Mapped[str | None] = mapped_column(String(32))
    observed_build_digest: Mapped[str | None] = mapped_column(String(71))
    observed_protocol_version: Mapped[int | None] = mapped_column(Integer)
    observed_active_slot: Mapped[str | None] = mapped_column(String(1))
    route_withdrawal_evidence_digest: Mapped[str | None] = mapped_column(String(64))
    acceptance_evidence_digest: Mapped[str | None] = mapped_column(String(64))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    failure_evidence_digest: Mapped[str | None] = mapped_column(String(64))
    rollback_evidence_digest: Mapped[str | None] = mapped_column(String(64))
    soak_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activation_deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UpdateAuthorizationIntent(Base):
    """Immutable reserve/sign/queue binding for one privileged agent operation."""

    __tablename__ = "update_authorization_intents"
    __table_args__ = (
        CheckConstraint(
            "action IN ('agent.update', 'agent.rollback')",
            name="ck_update_authorization_intents_action",
        ),
        CheckConstraint(
            "state IN ('reserved', 'signed', 'queued', 'stale')",
            name="ck_update_authorization_intents_state",
        ),
        CheckConstraint(
            "source_slot IN ('A', 'B')",
            name="ck_update_authorization_intents_source_slot",
        ),
        CheckConstraint(
            _lower_hex("payload_digest", 64),
            name="ck_update_authorization_intents_payload_digest",
        ),
        CheckConstraint(
            _lower_hex("source_sha256", 64),
            name="ck_update_authorization_intents_source_sha256",
        ),
        CheckConstraint(
            _lower_hex("request_digest", 64),
            name="ck_update_authorization_intents_request_digest",
        ),
        CheckConstraint(
            _nullable_lower_hex("response_digest", 64),
            name="ck_update_authorization_intents_response_digest",
        ),
        CheckConstraint(
            _lower_hex("admin_grant_digest", 64),
            name="ck_update_authorization_intents_admin_grant_digest",
        ),
        CheckConstraint(
            "target_release_digest IS NULL OR "
            "(length(target_release_digest) = 71 AND "
            "substr(target_release_digest, 1, 7) = 'sha256:' AND "
            f"({_lower_hex('substr(target_release_digest, 8, 64)', 64)}))",
            name="ck_update_authorization_intents_target_release_digest",
        ),
        CheckConstraint(
            _nullable_lower_hex("expected_tuf_target_sha256", 64),
            name="ck_update_authorization_intents_tuf_target_sha256",
        ),
        CheckConstraint(
            "(action = 'agent.update' AND target_release_digest IS NOT NULL AND "
            "expected_tuf_target_sha256 IS NOT NULL AND "
            "expected_tuf_targets_version IS NOT NULL AND "
            "expected_tuf_targets_version >= 1) OR "
            "(action = 'agent.rollback' AND target_release_digest IS NULL AND "
            "expected_tuf_target_sha256 IS NULL AND "
            "expected_tuf_targets_version IS NULL)",
            name="ck_update_authorization_intents_tuf_binding",
        ),
        CheckConstraint(
            "(state = 'reserved' AND signed_response IS NULL AND response_digest IS NULL "
            "AND queued_at IS NULL) OR "
            "(state = 'signed' AND signed_response IS NOT NULL AND response_digest IS NOT NULL "
            "AND queued_at IS NULL) OR "
            "(state = 'queued' AND signed_response IS NOT NULL AND response_digest IS NOT NULL "
            "AND queued_at IS NOT NULL) OR state = 'stale'",
            name="ck_update_authorization_intents_state_payload",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    rollout_id: Mapped[str] = mapped_column(
        ForeignKey("update_rollouts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rollout_node_id: Mapped[str] = mapped_column(
        ForeignKey("update_rollout_nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id"), nullable=False, index=True
    )
    operation_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    fence: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    unsigned_payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_slot: Mapped[str] = mapped_column(String(1), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    target_release_digest: Mapped[str | None] = mapped_column(String(71))
    expected_tuf_target_sha256: Mapped[str | None] = mapped_column(String(64))
    expected_tuf_targets_version: Mapped[int | None] = mapped_column(Integer)
    admin_grant: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    admin_grant_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    request: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    signed_response: Mapped[dict[str, object] | None] = mapped_column(JSON)
    response_digest: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PackageCandidate(Base):
    """A metadata-only observation of an upstream workload release.

    Candidate rows deliberately contain identities and bounded summaries only.
    The release lock, source credentials, and package bytes remain outside the
    operational database (in Git/TUF and the provider stores respectively).
    """

    __tablename__ = "package_candidates"
    __table_args__ = (
        CheckConstraint(
            "length(family_id) BETWEEN 1 AND 128",
            name="ck_package_candidates_family_id_length",
        ),
        CheckConstraint(
            _lower_hex("upstream_identity_digest", 64),
            name="ck_package_candidates_upstream_identity_digest",
        ),
        CheckConstraint(
            _lower_hex("metadata_digest", 64),
            name="ck_package_candidates_metadata_digest",
        ),
        CheckConstraint(
            "state IN ('discovered', 'resolving', 'resolved', 'unsupported', "
            "'quarantined', 'rejected')",
            name="ck_package_candidates_state",
        ),
        CheckConstraint(
            "reason_code IS NULL OR length(reason_code) BETWEEN 1 AND 80",
            name="ck_package_candidates_reason_code_length",
        ),
        CheckConstraint(
            "reason_detail IS NULL OR length(CAST(reason_detail AS TEXT)) <= 8192",
            name="ck_package_candidates_reason_detail_size",
        ),
        CheckConstraint(
            "length(source_provider) BETWEEN 1 AND 64",
            name="ck_package_candidates_source_provider_length",
        ),
        CheckConstraint(
            "length(source_reference) BETWEEN 1 AND 1024",
            name="ck_package_candidates_source_reference_length",
        ),
        UniqueConstraint(
            "family_id",
            "upstream_identity_digest",
            "metadata_digest",
            name="uq_package_candidates_identity",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    family_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    upstream_identity_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    upstream_version: Mapped[str] = mapped_column(String(256), nullable=False)
    channel: Mapped[str | None] = mapped_column(String(128))
    source_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    source_reference: Mapped[str] = mapped_column(String(1024), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    reason_code: Mapped[str | None] = mapped_column(String(80))
    reason_detail: Mapped[dict[str, object] | None] = mapped_column(JSON)
    summary: Mapped[dict[str, object] | None] = mapped_column(JSON)
    discovered_by: Mapped[str] = mapped_column(String(200), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class PackageResolution(Base):
    """A deterministic, retry-safe resolution projection for one candidate."""

    __tablename__ = "package_resolutions"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'resolving', 'resolved', 'unsupported', "
            "'incompatible', 'quarantined', 'rejected')",
            name="ck_package_resolutions_state",
        ),
        CheckConstraint(
            "resolver_schema_version >= 1",
            name="ck_package_resolutions_schema_version",
        ),
        CheckConstraint(
            _nullable_lower_hex("release_digest", 64),
            name="ck_package_resolutions_release_digest",
        ),
        CheckConstraint(
            "(state = 'resolved' AND release_digest IS NOT NULL) OR "
            "(state <> 'resolved')",
            name="ck_package_resolutions_resolved_release_binding",
        ),
        CheckConstraint(
            "reason_code IS NULL OR length(reason_code) BETWEEN 1 AND 80",
            name="ck_package_resolutions_reason_code_length",
        ),
        CheckConstraint(
            "reason_detail IS NULL OR length(CAST(reason_detail AS TEXT)) <= 8192",
            name="ck_package_resolutions_reason_detail_size",
        ),
        UniqueConstraint(
            "candidate_id",
            "resolver_id",
            "resolver_schema_version",
            name="uq_package_resolutions_candidate_resolver_schema",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("package_candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    resolver_id: Mapped[str] = mapped_column(String(128), nullable=False)
    resolver_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    release_digest: Mapped[str | None] = mapped_column(String(64), index=True)
    reason_code: Mapped[str | None] = mapped_column(String(80))
    reason_detail: Mapped[dict[str, object] | None] = mapped_column(JSON)
    summary: Mapped[dict[str, object] | None] = mapped_column(JSON)
    resolved_by: Mapped[str] = mapped_column(String(200), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class PackageValidationRun(Base):
    """Durable validation evidence bound to exact package and fleet digests."""

    __tablename__ = "package_validation_runs"
    __table_args__ = (
        CheckConstraint(
            "state IN ('planned', 'running', 'passed', 'failed', 'retryable', "
            "'rejected', 'cancelled')",
            name="ck_package_validation_runs_state",
        ),
        CheckConstraint(
            "validation_kind IN ('artifact', 'health', 'inference', 'compatibility')",
            name="ck_package_validation_runs_kind",
        ),
        CheckConstraint(
            _lower_hex("release_digest", 64),
            name="ck_package_validation_runs_release_digest",
        ),
        CheckConstraint(
            _lower_hex("policy_digest", 64),
            name="ck_package_validation_runs_policy_digest",
        ),
        CheckConstraint(
            _lower_hex("fleet_digest", 64),
            name="ck_package_validation_runs_fleet_digest",
        ),
        CheckConstraint(
            "attempt >= 0",
            name="ck_package_validation_runs_attempt",
        ),
        CheckConstraint(
            "reason_code IS NULL OR length(reason_code) BETWEEN 1 AND 80",
            name="ck_package_validation_runs_reason_code_length",
        ),
        CheckConstraint(
            "failure_detail IS NULL OR length(CAST(failure_detail AS TEXT)) <= 8192",
            name="ck_package_validation_runs_failure_detail_size",
        ),
        CheckConstraint(
            "evidence IS NULL OR length(CAST(evidence AS TEXT)) <= 16384",
            name="ck_package_validation_runs_evidence_size",
        ),
        CheckConstraint(
            "progress IS NULL OR length(CAST(progress AS TEXT)) <= 8192",
            name="ck_package_validation_runs_progress_size",
        ),
        UniqueConstraint(
            "resolution_id",
            "validation_kind",
            "policy_digest",
            "fleet_digest",
            name="uq_package_validation_runs_binding",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("package_candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    resolution_id: Mapped[str] = mapped_column(
        ForeignKey("package_resolutions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    validation_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    release_digest: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    policy_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    fleet_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(80))
    failure_detail: Mapped[dict[str, object] | None] = mapped_column(JSON)
    evidence: Mapped[dict[str, object] | None] = mapped_column(JSON)
    progress: Mapped[dict[str, object] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PackageRollout(Base):
    """Immutable digest-bound desired-state rollout and orchestration cursor."""

    __tablename__ = "package_rollouts"
    __table_args__ = (
        CheckConstraint(
            "state IN ('planned', 'preparing', 'activating', 'health-checking', "
            "'soaking', 'paused', 'rolling-back', 'completed', 'failed', "
            "'rolled-back', 'cancelled', 'running', 'partial', 'waiting-for-operator')",
            name="ck_package_rollouts_state",
        ),
        CheckConstraint(
            _lower_hex("deployment_digest", 64),
            name="ck_package_rollouts_deployment_digest",
        ),
        CheckConstraint(
            _lower_hex("release_digest", 64),
            name="ck_package_rollouts_release_digest",
        ),
        CheckConstraint(
            _nullable_lower_hex("previous_release_digest", 64),
            name="ck_package_rollouts_previous_release_digest",
        ),
        CheckConstraint(
            _lower_hex("policy_digest", 64),
            name="ck_package_rollouts_policy_digest",
        ),
        CheckConstraint(
            _lower_hex("tuf_target_digest", 64),
            name="ck_package_rollouts_tuf_target_digest",
        ),
        CheckConstraint(
            _lower_hex("fleet_digest", 64),
            name="ck_package_rollouts_fleet_digest",
        ),
        CheckConstraint(
            _lower_hex("topology_digest", 64),
            name="ck_package_rollouts_topology_digest",
        ),
        CheckConstraint(
            _lower_hex("plan_digest", 64),
            name="ck_package_rollouts_plan_digest",
        ),
        CheckConstraint(
            "base_commit IS NULL OR length(base_commit) BETWEEN 40 AND 128",
            name="ck_package_rollouts_base_commit_length",
        ),
        CheckConstraint(
            _lower_hex("authority_digest", 64),
            name="ck_package_rollouts_authority_digest",
        ),
        CheckConstraint(
            "(base_commit IS NOT NULL AND recipe_revision_id IS NULL) OR "
            "(base_commit IS NULL AND recipe_revision_id IS NOT NULL)",
            name="ck_package_rollouts_authority_kind",
        ),
        CheckConstraint(
            "current_batch >= 0",
            name="ck_package_rollouts_current_batch",
        ),
        CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) <= 1024",
            name="ck_package_rollouts_failure_reason_size",
        ),
        CheckConstraint(
            "plan IS NULL OR length(CAST(plan AS TEXT)) <= 32768",
            name="ck_package_rollouts_plan_size",
        ),
        CheckConstraint(
            "progress IS NULL OR length(CAST(progress AS TEXT)) <= 16384",
            name="ck_package_rollouts_progress_size",
        ),
        CheckConstraint(
            "failure_evidence_digest IS NULL OR "
            f"({_lower_hex('failure_evidence_digest', 64)})",
            name="ck_package_rollouts_failure_evidence_digest",
        ),
        CheckConstraint(
            "rollback_evidence_digest IS NULL OR "
            f"({_lower_hex('rollback_evidence_digest', 64)})",
            name="ck_package_rollouts_rollback_evidence_digest",
        ),
        UniqueConstraint(
            "deployment_id",
            "authority_digest",
            "plan_digest",
            name="uq_package_rollouts_deployment_authority_plan",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), unique=True
    )
    deployment_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    deployment_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    release_digest: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    previous_release_digest: Mapped[str | None] = mapped_column(String(64))
    base_commit: Mapped[str | None] = mapped_column(String(128))
    recipe_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("local_recipe_revisions.id", ondelete="RESTRICT"), index=True
    )
    authority_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    tuf_target_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    fleet_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    topology_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    plan: Mapped[dict[str, object] | None] = mapped_column(JSON)
    progress: Mapped[dict[str, object] | None] = mapped_column(JSON)
    current_batch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    failure_reason: Mapped[str | None] = mapped_column(Text)
    failure_evidence_digest: Mapped[str | None] = mapped_column(String(64))
    rollback_evidence_digest: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


@event.listens_for(PackageRollout, "before_insert")
def _populate_rollout_authority_digest(
    _mapper, _connection, target: PackageRollout
) -> None:
    if target.authority_digest:
        return
    if target.recipe_revision_id is None:
        authority = {
            "base_commit": target.base_commit,
            "deployment_digest": target.deployment_digest,
            "release_digest": target.release_digest,
        }
    else:
        authority = {
            "recipe_revision_id": target.recipe_revision_id,
            "deployment_digest": target.deployment_digest,
            "release_digest": target.release_digest,
            "plan_digest": target.plan_digest,
        }
    target.authority_digest = hashlib.sha256(
        json.dumps(authority, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class PackageRolloutNode(Base):
    """Per-node package operation binding and bounded progress projection."""

    __tablename__ = "package_rollout_nodes"
    __table_args__ = (
        UniqueConstraint(
            "rollout_id", "node_id", name="uq_package_rollout_nodes_rollout_node"
        ),
        UniqueConstraint(
            "rollout_id",
            "batch_index",
            "node_order",
            name="uq_package_rollout_nodes_batch_order",
        ),
        CheckConstraint(
            "state IN ('offline-pending', 'pending', 'queued', 'running', 'preparing', 'prepared', "
            "'activating', 'health-checking', 'accepted', 'failed', "
            "'rolling-back', 'rolled-back', 'cancelled')",
            name="ck_package_rollout_nodes_state",
        ),
        CheckConstraint(
            "batch_index >= -1 AND node_order >= 0",
            name="ck_package_rollout_nodes_order",
        ),
        CheckConstraint(
            _lower_hex("expected_payload_digest", 64),
            name="ck_package_rollout_nodes_expected_payload_digest",
        ),
        CheckConstraint(
            _nullable_lower_hex("observed_release_digest", 64),
            name="ck_package_rollout_nodes_observed_release_digest",
        ),
        CheckConstraint(
            _nullable_lower_hex("evidence_digest", 64),
            name="ck_package_rollout_nodes_evidence_digest",
        ),
        CheckConstraint(
            _nullable_lower_hex("failure_evidence_digest", 64),
            name="ck_package_rollout_nodes_failure_evidence_digest",
        ),
        CheckConstraint(
            _nullable_lower_hex("rollback_evidence_digest", 64),
            name="ck_package_rollout_nodes_rollback_evidence_digest",
        ),
        CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) <= 1024",
            name="ck_package_rollout_nodes_failure_reason_size",
        ),
        CheckConstraint(
            "progress IS NULL OR length(CAST(progress AS TEXT)) <= 8192",
            name="ck_package_rollout_nodes_progress_size",
        ),
        CheckConstraint(
            "length(CAST(operation_history AS TEXT)) <= 16384",
            name="ck_package_rollout_nodes_operation_history_size",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    rollout_id: Mapped[str] = mapped_column(
        ForeignKey("package_rollouts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id"), nullable=False, index=True
    )
    batch_index: Mapped[int] = mapped_column(Integer, nullable=False)
    node_order: Mapped[int] = mapped_column(Integer, nullable=False)
    is_canary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    state: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    operation_kind: Mapped[str | None] = mapped_column(String(80))
    graph_operation_id: Mapped[str | None] = mapped_column(String(128))
    operation_key: Mapped[str | None] = mapped_column(String(128))
    operation_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    rollback_operation_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    operation_history: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    expected_payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_release_digest: Mapped[str | None] = mapped_column(String(64))
    evidence_digest: Mapped[str | None] = mapped_column(String(64))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    failure_evidence_digest: Mapped[str | None] = mapped_column(String(64))
    rollback_evidence_digest: Mapped[str | None] = mapped_column(String(64))
    progress: Mapped[dict[str, object] | None] = mapped_column(JSON)
    dispatch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activation_deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PackageObservation(Base):
    """Latest bounded authenticated package state reported by an agent."""

    __tablename__ = "package_observations"
    __table_args__ = (
        CheckConstraint(
            "length(deployment_id) BETWEEN 1 AND 128",
            name="ck_package_observations_deployment_id_length",
        ),
        CheckConstraint(
            _lower_hex("release_digest", 64),
            name="ck_package_observations_release_digest",
        ),
        CheckConstraint(
            _lower_hex("observation_digest", 64),
            name="ck_package_observations_observation_digest",
        ),
        CheckConstraint(
            "state IN ('unknown', 'prepared', 'active', 'healthy', 'stopped', "
            "'failed', 'rolling-back')",
            name="ck_package_observations_state",
        ),
        CheckConstraint(
            "summary IS NULL OR length(CAST(summary AS TEXT)) <= 8192",
            name="ck_package_observations_summary_size",
        ),
        UniqueConstraint(
            "node_id",
            "deployment_id",
            "release_digest",
            "observation_digest",
            name="uq_package_observations_identity",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    deployment_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    release_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    observation_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[str | None] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    summary: Mapped[dict[str, object] | None] = mapped_column(JSON)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PackageActionPlan(Base):
    """Durable preview/apply fence for package administration mutations.

    The preview digest is the identity of the canonical request projection;
    keeping that projection in PostgreSQL makes removal/GC and other plan-only
    endpoints restart-safe without storing release bytes or credentials.
    """

    __tablename__ = "package_action_plans"
    __table_args__ = (
        CheckConstraint(
            _lower_hex("plan_digest", 64),
            name="ck_package_action_plans_plan_digest",
        ),
        CheckConstraint(
            "action IN ('package.validate', 'package.promote', 'package.rollout', "
            "'package.rollback', 'package.repair', 'package.remove', 'package.gc')",
            name="ck_package_action_plans_action",
        ),
        CheckConstraint(
            "state IN ('planned', 'applying', 'applied', 'expired', 'failed')",
            name="ck_package_action_plans_state",
        ),
        CheckConstraint(
            "length(subject) BETWEEN 1 AND 128",
            name="ck_package_action_plans_subject_length",
        ),
        CheckConstraint(
            "length(CAST(request AS TEXT)) BETWEEN 2 AND 65536",
            name="ck_package_action_plans_request_size",
        ),
        CheckConstraint(
            "result IS NULL OR length(CAST(result AS TEXT)) <= 16384",
            name="ck_package_action_plans_result_size",
        ),
    )
    plan_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    request: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    actor: Mapped[str | None] = mapped_column(String(200))
    result: Mapped[dict[str, object] | None] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class PackageFamily(Base):
    __tablename__ = "package_families"
    __table_args__ = (
        CheckConstraint("schema_version >= 1", name="ck_package_families_schema"),
        CheckConstraint("length(id) BETWEEN 1 AND 128", name="ck_package_families_id"),
    )
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    provider_kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    definition: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


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
    resolution_seconds: Mapped[int] = mapped_column(
        SmallInteger, primary_key=True
    )
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
    resolution_seconds: Mapped[int] = mapped_column(
        SmallInteger, primary_key=True
    )
    node_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    bucket_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    metric_name: Mapped[str] = mapped_column(String(64), primary_key=True)
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
    resolution_seconds: Mapped[int] = mapped_column(
        SmallInteger, primary_key=True
    )
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
        CheckConstraint(
            "singleton_id = 1", name="ck_fleet_event_cursor_singleton"
        ),
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
            "event_type IN ('node-telemetry','recipe-state','operation-state')",
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
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    recipe_revision_id: Mapped[str] = mapped_column(
        ForeignKey("local_recipe_revisions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
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
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
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
    alias: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
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
    updated_at: Mapped[datetime] = mapped_column(
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
