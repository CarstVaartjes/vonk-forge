"""Models: cache."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    literal_column,
)
from sqlalchemy.orm import Mapped, mapped_column
from vonk_agent_protocol import LifecycleState, LifecycleSubject

from ..exact_integer_storage import (
    DecimalIntegerAtMost,
    DecimalIntegerToken,
    ExactNonnegativeInteger,
)
from ..model_primitives import (
    Base,
    _lower_hex,
    _nullable_lower_hex,
    _state_in,
    _uuid_shape,
)


class ModelCacheSet(Base):
    """Immutable model artifact-set identity and its current NAS projection."""

    __tablename__ = "model_cache_sets"
    __table_args__ = (
        CheckConstraint(
            "schema_version = 2", name="ck_model_cache_sets_schema_version"
        ),
        CheckConstraint(
            _lower_hex("artifact_set_sha256", 64),
            name="ck_model_cache_sets_artifact_set_digest",
        ),
        CheckConstraint(
            "model_content_sha256 IS NULL OR "
            f"({_lower_hex('model_content_sha256', 64)})",
            name="ck_model_cache_sets_model_content_digest",
        ),
        CheckConstraint(
            "recipe_revision_sha256 IS NULL OR "
            f"({_lower_hex('recipe_revision_sha256', 64)})",
            name="ck_model_cache_sets_recipe_revision_digest",
        ),
        CheckConstraint(
            "state IN ('incomplete','downloading','verifying','cached',"
            "'needs-repair','failed')",
            name="ck_model_cache_sets_state",
        ),
        CheckConstraint(
            DecimalIntegerToken(literal_column("expected_bytes"))
            & DecimalIntegerToken(literal_column("verified_bytes"))
            & DecimalIntegerAtMost(
                literal_column("verified_bytes"), literal_column("expected_bytes")
            ),
            name="ck_model_cache_sets_sizes",
        ),
        CheckConstraint(
            "length(CAST(manifest AS TEXT)) BETWEEN 2 AND 1048576",
            name="ck_model_cache_sets_manifest_size",
        ),
    )
    artifact_set_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    model_content_sha256: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    recipe_revision_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    manifest: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    expected_bytes: Mapped[int] = mapped_column(
        ExactNonnegativeInteger(), nullable=False
    )
    verified_bytes: Mapped[int] = mapped_column(
        ExactNonnegativeInteger(), nullable=False, default=0
    )
    state: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    protected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    protected_reasons: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_accessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    last_error: Mapped[str | None] = mapped_column(String(512))


class ModelCacheSetArtifact(Base):
    """Stable membership projection; one artifact object may serve many sets."""

    __tablename__ = "model_cache_set_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "artifact_set_sha256",
            "artifact_key",
            name="uq_model_cache_set_artifact_key",
        ),
        CheckConstraint(
            "length(artifact_key) BETWEEN 1 AND 256",
            name="ck_model_cache_set_artifacts_key",
        ),
        CheckConstraint(
            "length(path) BETWEEN 1 AND 512",
            name="ck_model_cache_set_artifacts_path",
        ),
    )
    artifact_set_sha256: Mapped[str] = mapped_column(
        ForeignKey("model_cache_sets.artifact_set_sha256", ondelete="CASCADE"),
        primary_key=True,
    )
    artifact_key: Mapped[str] = mapped_column(String(256), primary_key=True)
    # The object's bytes and verification receipt are a managed-storage fact,
    # so membership records the exact digest without a SQL availability row.
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    path: Mapped[str] = mapped_column(String(512), nullable=False)


class ModelCacheOperation(Base):
    """Restart-safe NAS operation with per-artifact byte checkpoints."""

    __tablename__ = "model_cache_operations"
    __table_args__ = (
        CheckConstraint(
            "schema_version = 2", name="ck_model_cache_operations_schema_version"
        ),
        CheckConstraint(
            "kind IN ('download','repair','remove')",
            name="ck_model_cache_operations_kind",
        ),
        CheckConstraint(
            _state_in(
                LifecycleSubject.MODEL_CACHE_OPERATION,
                LifecycleState.QUEUED,
                LifecycleState.RUNNING,
                LifecycleState.BACKOFF,
                LifecycleState.SUCCEEDED,
                LifecycleState.FAILED,
                LifecycleState.CANCELLED,
            ),
            name="ck_model_cache_operations_state",
        ),
        CheckConstraint("attempt >= 1", name="ck_model_cache_operations_attempt"),
        CheckConstraint(
            f"plan_digest IS NULL OR ({_lower_hex('plan_digest', 64)})",
            name="ck_model_cache_operations_plan_digest",
        ),
        CheckConstraint(
            "length(CAST(progress AS TEXT)) BETWEEN 2 AND 1048576",
            name="ck_model_cache_operations_progress_size",
        ),
        CheckConstraint(
            "length(CAST(payload AS TEXT)) BETWEEN 2 AND 262144",
            name="ck_model_cache_operations_payload_size",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    request_key: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    artifact_set_sha256: Mapped[str | None] = mapped_column(
        ForeignKey("model_cache_sets.artifact_set_sha256", ondelete="SET NULL"),
        index=True,
    )
    plan_digest: Mapped[str | None] = mapped_column(String(64), index=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    progress: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    current_artifact_key: Mapped[str | None] = mapped_column(String(256))
    last_error: Mapped[str | None] = mapped_column(String(512))
    #: The one retry, observation and cancel clock of the lifecycle core: when a
    #: queued or interrupted operation may be claimed again.  It replaces the
    #: payload's ``retry.next_retry_at``/``retry_after_seconds`` (read only by the
    #: lifecycle adapter's ``adopt`` for rows written before the core).
    next_action_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    #: How many times the core looked at a cancelled operation since it last ran
    #: (a stop and its confirmation); the stop budget of a cancel.
    observe_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    #: The claim: which Controller process runs the operation and until when.  It
    #: replaces the payload's ``claim`` (owner and expiry).  A lapsed lease is
    #: decided by the core, not stolen by the next claimant.
    fence: Mapped[str | None] = mapped_column(String(64))
    lease_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ArtifactLifecycleGate(Base):
    """SQL deletion fence for one exact managed model or image object.

    This row coordinates reference acquisition with managed-storage deletion.
    It records neither byte availability nor a duplicate list of references;
    existing request, profile, workload, and distribution owners remain the
    authorities for those facts.
    """

    __tablename__ = "artifact_lifecycle_gates"
    __table_args__ = (
        CheckConstraint(
            "artifact_kind IN ('model-set','model-object','runtime-image')",
            name="ck_artifact_lifecycle_gates_kind",
        ),
        CheckConstraint(
            _lower_hex("artifact_sha256", 64),
            name="ck_artifact_lifecycle_gates_digest",
        ),
        CheckConstraint(
            "(removal_owner_kind IS NULL AND removal_owner_id IS NULL "
            "AND removal_fence IS NULL) OR "
            "(removal_owner_kind IN ('model-cache-operation','recipe-image-job') "
            "AND removal_owner_id IS NOT NULL AND removal_fence IS NOT NULL)",
            name="ck_artifact_lifecycle_gates_removal_owner",
        ),
        CheckConstraint(
            "removal_owner_id IS NULL OR (" + _uuid_shape("removal_owner_id") + ")",
            name="ck_artifact_lifecycle_gates_removal_owner_id",
        ),
        CheckConstraint(
            "removal_fence IS NULL OR (" + _uuid_shape("removal_fence") + ")",
            name="ck_artifact_lifecycle_gates_removal_fence",
        ),
    )
    artifact_kind: Mapped[str] = mapped_column(String(24), primary_key=True)
    artifact_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    removal_owner_kind: Mapped[str | None] = mapped_column(String(32))
    removal_owner_id: Mapped[str | None] = mapped_column(String(36))
    removal_fence: Mapped[str | None] = mapped_column(String(36))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


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
    # Accepted request semantics are independent of disposable result progress.
    reviewed_content_sha256: Mapped[str | None] = mapped_column(String(64))
    observed_commit: Mapped[str | None] = mapped_column(String(40), index=True)
    # The recipe library release (its contract version) and when its recipes
    # last changed, read from the signed catalog index.
    library_version: Mapped[str | None] = mapped_column(String(32))
    library_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    # The Controller version and recipe contract that produced this result.
    controller_marker: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Last progress of a running sync; a stale one is dead.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
        ForeignKey(
            "catalog_document_revisions.id",
            name="fk_recipe_builds_canonical_recipe_revision",
            ondelete="RESTRICT",
        ),
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
