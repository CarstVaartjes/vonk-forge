"""Persist Controller verified runtime image identity and provenance."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020_runtime_image_receipts"
down_revision = "0019_recipe_builds_revision_fk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_image_receipts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("recipe_revision_id", sa.String(36), nullable=False),
        sa.Column("source_type", sa.String(16), nullable=False),
        sa.Column("recipe_publisher", sa.String(63), nullable=False),
        sa.Column("recipe_slug", sa.String(63), nullable=False),
        sa.Column("original_content_digest", sa.String(64), nullable=False),
        sa.Column("effective_execution_key", sa.String(64), nullable=False),
        sa.Column("image_digest", sa.String(71), nullable=False),
        sa.Column("registry_parent", sa.String(255)),
        sa.Column("selected_platform_manifest", sa.JSON, nullable=False),
        sa.Column("config_id", sa.String(128), nullable=False),
        sa.Column("archive_sha256", sa.String(64)),
        sa.Column("archive_bytes", sa.BigInteger),
        sa.Column("architecture", sa.String(32), nullable=False),
        sa.Column("interface", sa.String(64), nullable=False),
        sa.Column("build_id", sa.String(36)),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.ForeignKeyConstraint(
            ["recipe_revision_id"],
            ["catalog_document_revisions.id"],
            name="fk_runtime_image_receipts_recipe_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["build_id"],
            ["recipe_builds.id"],
            name="fk_runtime_image_receipts_build",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "recipe_revision_id",
            "source_type",
            "original_content_digest",
            "effective_execution_key",
            "image_digest",
            name="uq_runtime_image_receipt_identity",
        ),
        sa.CheckConstraint(
            "source_type IN ('registry','source-build','archive')",
            name="ck_runtime_image_receipts_source_type",
        ),
        sa.CheckConstraint(
            "state IN ('verified','revoked')",
            name="ck_runtime_image_receipts_state",
        ),
        sa.CheckConstraint(
            "length(original_content_digest) = 64 AND original_content_digest = lower(original_content_digest)",
            name="ck_runtime_image_receipts_original_digest",
        ),
        sa.CheckConstraint(
            "length(effective_execution_key) = 64 AND effective_execution_key = lower(effective_execution_key)",
            name="ck_runtime_image_receipts_execution_key",
        ),
        sa.CheckConstraint(
            "length(image_digest) = 71 AND substr(image_digest, 1, 7) = 'sha256:'",
            name="ck_runtime_image_receipts_image_digest",
        ),
        sa.CheckConstraint(
            "archive_sha256 IS NULL OR (length(archive_sha256) = 64 AND archive_sha256 = lower(archive_sha256))",
            name="ck_runtime_image_receipts_archive_digest",
        ),
        sa.CheckConstraint(
            "archive_bytes IS NULL OR archive_bytes > 0",
            name="ck_runtime_image_receipts_archive_bytes",
        ),
        sa.CheckConstraint(
            "(archive_sha256 IS NULL AND archive_bytes IS NULL) OR (archive_sha256 IS NOT NULL AND archive_bytes IS NOT NULL)",
            name="ck_runtime_image_receipts_archive_pair",
        ),
        sa.CheckConstraint(
            "source_type != 'source-build' OR build_id IS NOT NULL",
            name="ck_runtime_image_receipts_source_build",
        ),
    )
    for column in (
        "recipe_revision_id",
        "source_type",
        "recipe_publisher",
        "recipe_slug",
        "original_content_digest",
        "effective_execution_key",
        "image_digest",
        "archive_sha256",
        "build_id",
        "state",
    ):
        op.create_index(
            f"ix_runtime_image_receipts_{column}",
            "runtime_image_receipts",
            [column],
        )


def downgrade() -> None:
    for column in (
        "state",
        "build_id",
        "archive_sha256",
        "image_digest",
        "effective_execution_key",
        "original_content_digest",
        "recipe_slug",
        "recipe_publisher",
        "source_type",
        "recipe_revision_id",
    ):
        op.drop_index(
            f"ix_runtime_image_receipts_{column}",
            table_name="runtime_image_receipts",
        )
    op.drop_table("runtime_image_receipts")
