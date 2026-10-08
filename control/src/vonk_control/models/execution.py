"""Models: execution."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import JsonValue
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from vonk_agent_protocol import (
    ArtifactPreparation,
    InstallationNodeState,
    InstallationState,
    LifecycleState,
    LifecycleSubject,
    ReservationState,
    RouteState,
    RunState,
    machine_check,
)

from ..model_primitives import Base, _lower_hex, _nullable_lower_hex, _state_in


class RecipeInstallation(Base):
    __tablename__ = "recipe_installations"
    __table_args__ = (
        CheckConstraint(
            _lower_hex("plan_digest", 64), name="ck_recipe_installations_digest"
        ),
        CheckConstraint(
            machine_check(InstallationState),
            name="ck_recipe_installations_state",
        ),
        CheckConstraint(
            _nullable_lower_hex("model_content_sha256", 64),
            name="ck_recipe_installations_model_content_sha256",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    recipe_revision_id: Mapped[str] = mapped_column(
        ForeignKey("catalog_document_revisions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # Persist the exact primary model content identity accepted with the immutable
    # recipe revision.  This makes model-to-installation ownership explicit
    # without attempting to infer it from mutable catalog display metadata.
    model_content_sha256: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    mapping_id: Mapped[str] = mapped_column(
        ForeignKey("cluster_mappings.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    mapping_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    recipe_build_id: Mapped[str | None] = mapped_column(
        ForeignKey("recipe_builds.id", ondelete="RESTRICT"), nullable=True, index=True
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
        CheckConstraint(
            machine_check(InstallationNodeState),
            name="ck_installation_nodes_state",
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
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


#: Stoppable runs that no running-run path (observation, recovery) advances.
STOPPABLE_NOT_RUNNING_RUN_STATES = frozenset(
    {RunState.PLANNED, RunState.STARTING, RunState.STOPPING, RunState.LOST}
)
ACTIVE_RUN_STATES = (STOPPABLE_NOT_RUNNING_RUN_STATES - {RunState.LOST}) | {
    RunState.RUNNING
}
#: The run states that own capacity and a Spark lifecycle: any load that needs
#: such a run's Sparks plans its exact Stop, which releases the claims. A run in
#: any other state can never be stopped by a plan, so it holds no ports or
#: memory and is not wanted on any Spark.
STOPPABLE_RUN_STATES = ACTIVE_RUN_STATES | {RunState.LOST}


class RecipeRun(Base):
    __tablename__ = "recipe_runs"
    __table_args__ = (
        CheckConstraint(_lower_hex("plan_digest", 64), name="ck_recipe_runs_digest"),
        CheckConstraint(
            machine_check(RunState),
            name="ck_recipe_runs_state",
        ),
        CheckConstraint(
            machine_check(RouteState, "route_state"),
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
        CheckConstraint(
            "route_attempts>=0",
            name="ck_recipe_runs_route_attempts",
        ),
        CheckConstraint(
            "recovery_attempts>=0",
            name="ck_recipe_runs_recovery_attempts",
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
        String(24),
        nullable=False,
        default=RouteState.WITHDRAWN.value,
        server_default=RouteState.WITHDRAWN.value,
    )
    route_generation: Mapped[int | None] = mapped_column(BigInteger)
    route_digest: Mapped[str | None] = mapped_column(String(64))
    route_error: Mapped[str | None] = mapped_column(String(512))
    #: Publication attempts already spent on this run and the durable time the
    #: next one becomes eligible.  A temporary publication failure keeps
    #: ``route_state='pending'`` and records when to try again instead of
    #: turning one supervisor hiccup into a terminal route failure.
    route_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    #: Recovery retries in the current bounded window. The coordinator resets
    #: this after the visible cooldown so transient faults resume automatically.
    recovery_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    route_next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
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
    observed_run_generation: Mapped[int | None] = mapped_column(BigInteger)
    #: Process presence from the agent's current-generation observation.
    #: ``None`` means no current observation has been applied.
    observation_process_running: Mapped[bool | None] = mapped_column(Boolean)
    #: Observation time is separate from row updates so unrelated lifecycle
    #: writes cannot make an old absence observation look fresh.
    observation_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    observation_endpoint_ready: Mapped[bool | None] = mapped_column(Boolean)
    observation_failure_diagnostics: Mapped[JsonValue | None] = mapped_column(JSON)
    #: When a running endpoint owner's own readiness probe first failed in
    #: the current unbroken run of failures; ``None`` while it answers.
    observation_unready_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
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
            _state_in(
                LifecycleSubject.ARTIFACT_JOB,
                *(
                    state
                    for state in LifecycleState
                    if state is not LifecycleState.BACKOFF
                ),
                extra=tuple(stage.value for stage in ArtifactPreparation),
                nullable=True,
            ),
            name="ck_artifact_jobs_state",
        ),
        CheckConstraint(
            "preparation IS NULL OR preparation IN ('draft','ready')",
            name="ck_artifact_jobs_preparation",
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
    #: The lifecycle state; ``NULL`` until the job is submitted (it is preparing).
    state: Mapped[str | None] = mapped_column(String(24), index=True)
    #: ``draft`` while inputs upload, ``ready`` once complete; ``NULL`` after submit.
    preparation: Mapped[str | None] = mapped_column(String(8))
    #: When a cancel was requested (monotonic); the job's state stays the core's.
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    input_manifest: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    input_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    input_total_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    output_manifest_sha256: Mapped[str | None] = mapped_column(String(64))
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
            f"{machine_check(ReservationState)} AND amount_bytes>=0",
            name="ck_reservations_state",
        ),
        CheckConstraint(
            "state!='promised' OR (kind IN "
            "('port','unified-memory','host-memory','gpu-memory') "
            "AND owner_kind='fleet-profile')",
            name="ck_reservations_promised_owner",
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
        Index(
            "uq_promised_node_port",
            "node_id",
            "kind",
            "resource_key",
            unique=True,
            postgresql_where=text("state='promised' AND kind='port'"),
            sqlite_where=text("state='promised' AND kind='port'"),
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
