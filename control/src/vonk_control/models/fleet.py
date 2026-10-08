"""Models: fleet."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import JsonValue
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from vonk_agent_protocol import LifecycleState, LifecycleSubject

from ..model_primitives import Base, _lower_hex, _nullable_lower_hex, _state_in


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
            "architecture IS NULL OR architecture IN ('linux-amd64', 'linux-arm64')",
            name="ck_agent_nodes_architecture",
        ),
        CheckConstraint(
            _nullable_lower_hex("preflight_fingerprint", 64),
            name="ck_agent_nodes_preflight_fingerprint",
        ),
    )
    node_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workload_intent_ordinal: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    protocol_version: Mapped[int | None] = mapped_column(Integer)
    architecture: Mapped[str | None] = mapped_column(String(16))
    semantic_version: Mapped[str | None] = mapped_column(String(32))
    build_digest: Mapped[str | None] = mapped_column(String(71))
    binary_digest: Mapped[str | None] = mapped_column(String(64))
    contact_certificate_serial: Mapped[str | None] = mapped_column(String(128))
    contact_observation_digest: Mapped[str | None] = mapped_column(String(64))
    # The host-runtime fingerprint the agent's last claim reported; a runtime
    # preflight proof is current only while it matches.
    preflight_fingerprint: Mapped[str | None] = mapped_column(String(64))
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
    """Numbered whole-fleet workspace of logical recipe choices.

    ``assignments`` is authoring data only.  It deliberately contains recipe
    selectors and Spark IDs, never resolved recipe revisions.  Preview/load
    materializes the current enrolled roster and stores an immutable resolved
    execution snapshot in :class:`FleetProfileApplication`.
    """

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
            "number >= 1",
            name="ck_fleet_profiles_number_positive",
        ),
        CheckConstraint(
            "revision >= 1",
            name="ck_fleet_profiles_revision_positive",
        ),
        CheckConstraint(
            "length(CAST(assignments AS TEXT)) BETWEEN 2 AND 131072",
            name="ck_fleet_profiles_assignments_size",
        ),
        UniqueConstraint("number", name="uq_fleet_profiles_number"),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
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
            _state_in(
                LifecycleSubject.FLEET_PROFILE_APPLICATION,
                LifecycleState.QUEUED,
                LifecycleState.RUNNING,
                LifecycleState.NEEDS_OPERATOR,
                LifecycleState.SUCCEEDED,
                LifecycleState.FAILED,
                LifecycleState.CANCELLED,
                LifecycleState.SUPERSEDED,
            ),
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
        CheckConstraint(
            "selection_generation IS NULL OR selection_generation >= 1",
            name="ck_fleet_profile_applications_selection_generation",
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
    selection_generation: Mapped[int | None] = mapped_column(Integer)
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


class FleetProfileSelection(Base):
    """The one profile application currently selected for the whole fleet."""

    __tablename__ = "fleet_profile_selection"
    __table_args__ = (
        CheckConstraint(
            "singleton_id = 1",
            name="ck_fleet_profile_selection_singleton",
        ),
        CheckConstraint(
            "generation >= 1",
            name="ck_fleet_profile_selection_generation_positive",
        ),
        CheckConstraint(
            "profile_revision >= 1",
            name="ck_fleet_profile_selection_revision_positive",
        ),
        CheckConstraint(
            _lower_hex("roster_digest", 64),
            name="ck_fleet_profile_selection_roster_digest",
        ),
    )
    singleton_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    profile_id: Mapped[str] = mapped_column(
        ForeignKey("fleet_profiles.id", ondelete="RESTRICT"), nullable=False
    )
    profile_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("fleet_profile_applications.id", ondelete="RESTRICT"),
        nullable=False,
    )
    roster_digest: Mapped[str] = mapped_column(String(64), nullable=False)
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
    provider_request: Mapped[JsonValue | None] = mapped_column(JSON)
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
        CheckConstraint(
            "requested_display_name IS NULL OR "
            "length(requested_display_name) BETWEEN 1 AND 200",
            name="ck_agent_enrollment_grants_requested_display_name",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    node_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    purpose: Mapped[str] = mapped_column(
        String(24), nullable=False, default="new-node", server_default="new-node"
    )
    requested_display_name: Mapped[str | None] = mapped_column(String(200))
    token_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentEnrollment(Base):
    __tablename__ = "agent_enrollments"
    __table_args__ = (
        CheckConstraint(
            "state IN ('issuing', 'certificate_issued')",
            name="ck_agent_enrollments_state",
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
    provider_request: Mapped[JsonValue | None] = mapped_column(JSON)
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
