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
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("original_content_digest", sa.String(64), nullable=False),
        sa.Column("effective_execution_key", sa.String(64), nullable=False),
        sa.Column("registry_manifest_digest", sa.String(71)),
        sa.Column("platform_manifest_digest", sa.String(71), nullable=False),
        sa.Column("local_image_config_id", sa.String(71), nullable=False),
        sa.Column("oci_archive_sha256", sa.String(64)),
        sa.Column("image_bytes", sa.BigInteger),
        sa.Column("architecture", sa.String(32), nullable=False),
        sa.Column("runtime_interface", sa.String(64), nullable=False),
        sa.Column("runtime_interface_label", sa.String(128), nullable=False),
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
            "source",
            "original_content_digest",
            "effective_execution_key",
            "platform_manifest_digest",
            "local_image_config_id",
            name="uq_runtime_image_receipt_identity",
        ),
        sa.CheckConstraint(
            "source IN ('published','controller-build')",
            name="ck_runtime_image_receipts_source",
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
            "length(platform_manifest_digest) = 71 AND substr(platform_manifest_digest, 1, 7) = 'sha256:'",
            name="ck_runtime_image_receipts_platform_digest",
        ),
        sa.CheckConstraint(
            "registry_manifest_digest IS NULL OR (length(registry_manifest_digest) = 71 AND substr(registry_manifest_digest, 1, 7) = 'sha256:')",
            name="ck_runtime_image_receipts_registry_digest",
        ),
        sa.CheckConstraint(
            "length(local_image_config_id) = 71 AND substr(local_image_config_id, 1, 7) = 'sha256:'",
            name="ck_runtime_image_receipts_config_digest",
        ),
        sa.CheckConstraint(
            "oci_archive_sha256 IS NULL OR (length(oci_archive_sha256) = 64 AND oci_archive_sha256 = lower(oci_archive_sha256))",
            name="ck_runtime_image_receipts_archive_digest",
        ),
        sa.CheckConstraint(
            "image_bytes IS NULL OR image_bytes > 0",
            name="ck_runtime_image_receipts_image_bytes",
        ),
        sa.CheckConstraint(
            "(oci_archive_sha256 IS NULL AND image_bytes IS NULL) OR (oci_archive_sha256 IS NOT NULL AND image_bytes IS NOT NULL)",
            name="ck_runtime_image_receipts_archive_pair",
        ),
        sa.CheckConstraint(
            "(source = 'published' AND build_id IS NULL) OR (source = 'controller-build' AND build_id IS NOT NULL)",
            name="ck_runtime_image_receipts_source_build",
        ),
        sa.CheckConstraint(
            "source = 'published' OR (registry_manifest_digest IS NULL AND oci_archive_sha256 IS NOT NULL AND image_bytes IS NOT NULL)",
            name="ck_runtime_image_receipts_source_artifacts",
        ),
        sa.CheckConstraint(
            "length(architecture) BETWEEN 1 AND 32 AND length(runtime_interface) BETWEEN 1 AND 64 AND length(runtime_interface_label) BETWEEN 1 AND 128",
            name="ck_runtime_image_receipts_runtime_identity",
        ),
        sa.CheckConstraint(
            "state IN ('verified','revoked')",
            name="ck_runtime_image_receipts_state",
        ),
    )
    for column in (
        "recipe_revision_id",
        "source",
        "original_content_digest",
        "effective_execution_key",
        "platform_manifest_digest",
        "local_image_config_id",
        "registry_manifest_digest",
        "oci_archive_sha256",
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
        "oci_archive_sha256",
        "registry_manifest_digest",
        "local_image_config_id",
        "platform_manifest_digest",
        "effective_execution_key",
        "original_content_digest",
        "source",
        "recipe_revision_id",
    ):
        op.drop_index(
            f"ix_runtime_image_receipts_{column}",
            table_name="runtime_image_receipts",
        )
    op.drop_table("runtime_image_receipts")
