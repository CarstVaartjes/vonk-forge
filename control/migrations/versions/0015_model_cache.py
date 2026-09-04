"""Persist the Controller-owned schema-2 NAS model cache."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_model_cache"
down_revision = "0014_fleet_profile_scope"
branch_labels = None
depends_on = None


def _lower_hex(column: str, length: int) -> str:
    remainder = column
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return (
        f"length({column}) = {length} AND {column} = lower({column}) AND "
        f"length({remainder}) = 0"
    )


def upgrade() -> None:
    op.create_table(
        "model_cache_sets",
        sa.Column("artifact_set_sha256", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("model_version_sha256", sa.String(length=64), nullable=True),
        sa.Column("recipe_revision_sha256", sa.String(length=64), nullable=True),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("expected_bytes", sa.BigInteger(), nullable=False),
        sa.Column("verified_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("protected", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("protected_reasons", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.String(length=512), nullable=True),
        sa.CheckConstraint("schema_version = 2", name="ck_model_cache_sets_schema_version"),
        sa.CheckConstraint(
            _lower_hex("artifact_set_sha256", 64),
            name="ck_model_cache_sets_artifact_set_digest",
        ),
        sa.CheckConstraint(
            "model_version_sha256 IS NULL OR ("
            + _lower_hex("model_version_sha256", 64)
            + ")",
            name="ck_model_cache_sets_model_version_digest",
        ),
        sa.CheckConstraint(
            "recipe_revision_sha256 IS NULL OR ("
            + _lower_hex("recipe_revision_sha256", 64)
            + ")",
            name="ck_model_cache_sets_recipe_revision_digest",
        ),
        sa.CheckConstraint(
            "state IN ('incomplete','downloading','verifying','cached','needs-repair','failed')",
            name="ck_model_cache_sets_state",
        ),
        sa.CheckConstraint(
            "expected_bytes > 0 AND verified_bytes >= 0 AND verified_bytes <= expected_bytes",
            name="ck_model_cache_sets_sizes",
        ),
        sa.CheckConstraint(
            "length(CAST(manifest AS TEXT)) BETWEEN 2 AND 1048576",
            name="ck_model_cache_sets_manifest_size",
        ),
        sa.PrimaryKeyConstraint("artifact_set_sha256"),
    )
    op.create_index(
        "ix_model_cache_sets_model_version_sha256",
        "model_cache_sets",
        ["model_version_sha256"],
    )
    op.create_index(
        "ix_model_cache_sets_recipe_revision_sha256",
        "model_cache_sets",
        ["recipe_revision_sha256"],
    )
    op.create_index("ix_model_cache_sets_state", "model_cache_sets", ["state"])
    op.create_index("ix_model_cache_sets_updated_at", "model_cache_sets", ["updated_at"])
    op.create_index(
        "ix_model_cache_sets_last_accessed_at",
        "model_cache_sets",
        ["last_accessed_at"],
    )

    op.create_table(
        "model_cache_artifacts",
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("identity", sa.JSON(), nullable=False),
        sa.Column("storage_key", sa.String(length=255), nullable=False),
        sa.Column("expected_bytes", sa.BigInteger(), nullable=False),
        sa.Column("actual_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            _lower_hex("sha256", 64), name="ck_model_cache_artifacts_digest"
        ),
        sa.CheckConstraint(
            "expected_bytes > 0 AND actual_bytes >= 0 AND actual_bytes <= expected_bytes",
            name="ck_model_cache_artifacts_sizes",
        ),
        sa.CheckConstraint(
            "state IN ('partial','verified','missing','corrupt')",
            name="ck_model_cache_artifacts_state",
        ),
        sa.CheckConstraint(
            "length(CAST(identity AS TEXT)) BETWEEN 2 AND 65536",
            name="ck_model_cache_artifacts_identity_size",
        ),
        sa.PrimaryKeyConstraint("sha256"),
        sa.UniqueConstraint("storage_key"),
    )
    op.create_index("ix_model_cache_artifacts_state", "model_cache_artifacts", ["state"])
    op.create_index(
        "ix_model_cache_artifacts_updated_at", "model_cache_artifacts", ["updated_at"]
    )

    op.create_table(
        "model_cache_set_artifacts",
        sa.Column("artifact_set_sha256", sa.String(length=64), nullable=False),
        sa.Column("artifact_key", sa.String(length=256), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("path", sa.String(length=512), nullable=False),
        sa.Column("roles", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "length(artifact_key) BETWEEN 1 AND 256",
            name="ck_model_cache_set_artifacts_key",
        ),
        sa.CheckConstraint(
            "length(path) BETWEEN 1 AND 512",
            name="ck_model_cache_set_artifacts_path",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_set_sha256"],
            ["model_cache_sets.artifact_set_sha256"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_sha256"],
            ["model_cache_artifacts.sha256"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("artifact_set_sha256", "artifact_key"),
        sa.UniqueConstraint(
            "artifact_set_sha256", "artifact_key", name="uq_model_cache_set_artifact_key"
        ),
    )
    op.create_index(
        "ix_model_cache_set_artifacts_artifact_sha256",
        "model_cache_set_artifacts",
        ["artifact_sha256"],
    )

    op.create_table(
        "model_cache_operations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("request_key", sa.String(length=36), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("artifact_set_sha256", sa.String(length=64), nullable=True),
        sa.Column("plan_digest", sa.String(length=64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("progress", sa.JSON(), nullable=False),
        sa.Column("actor", sa.String(length=200), nullable=False),
        sa.Column("current_artifact_key", sa.String(length=256), nullable=True),
        sa.Column("last_error", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "schema_version = 2", name="ck_model_cache_operations_schema_version"
        ),
        sa.CheckConstraint(
            "kind IN ('download','repair','evict')",
            name="ck_model_cache_operations_kind",
        ),
        sa.CheckConstraint(
            "state IN ('queued','running','partial','succeeded','failed','cancelled')",
            name="ck_model_cache_operations_state",
        ),
        sa.CheckConstraint(
            "attempt >= 1", name="ck_model_cache_operations_attempt"
        ),
        sa.CheckConstraint(
            "plan_digest IS NULL OR (" + _lower_hex("plan_digest", 64) + ")",
            name="ck_model_cache_operations_plan_digest",
        ),
        sa.CheckConstraint(
            "length(CAST(progress AS TEXT)) BETWEEN 2 AND 65536",
            name="ck_model_cache_operations_progress_size",
        ),
        sa.CheckConstraint(
            "length(CAST(payload AS TEXT)) BETWEEN 2 AND 262144",
            name="ck_model_cache_operations_payload_size",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_set_sha256"],
            ["model_cache_sets.artifact_set_sha256"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_key"),
    )
    op.create_index("ix_model_cache_operations_kind", "model_cache_operations", ["kind"])
    op.create_index("ix_model_cache_operations_state", "model_cache_operations", ["state"])
    op.create_index(
        "ix_model_cache_operations_artifact_set_sha256",
        "model_cache_operations",
        ["artifact_set_sha256"],
    )
    op.create_index(
        "ix_model_cache_operations_plan_digest", "model_cache_operations", ["plan_digest"]
    )
    op.create_index(
        "ix_model_cache_operations_created_at", "model_cache_operations", ["created_at"]
    )


def downgrade() -> None:
    op.drop_table("model_cache_operations")
    op.drop_table("model_cache_set_artifacts")
    op.drop_table("model_cache_artifacts")
    op.drop_table("model_cache_sets")
