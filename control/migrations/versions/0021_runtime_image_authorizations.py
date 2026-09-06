"""Bind current recipe revisions to immutable runtime image receipts."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# PostgreSQL's fresh-baseline ``alembic_version`` column is varchar(32).
# Keep this identifier within that wire/schema limit while retaining the
# descriptive migration filename.
revision = "0021_runtime_authz"
down_revision = "0020_runtime_image_receipts"
branch_labels = None
depends_on = None


def _lower_hex(column: str, length: int) -> str:
    remainder = column
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return (
        f"length({column}) = {length} AND {column} = lower({column}) "
        f"AND length({remainder}) = 0"
    )


def _prefixed_digest(column: str) -> str:
    return (
        f"length({column}) = 71 AND substr({column}, 1, 7) = 'sha256:' "
        f"AND ({_lower_hex(f'substr({column}, 8)', 64)})"
    )


def upgrade() -> None:
    op.create_table(
        "runtime_image_authorizations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("recipe_revision_id", sa.String(36), nullable=False),
        sa.Column("receipt_id", sa.String(36), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("original_content_digest", sa.String(64), nullable=False),
        sa.Column("effective_execution_key", sa.String(64), nullable=False),
        sa.Column("registry_manifest_digest", sa.String(71)),
        sa.Column("platform_manifest_digest", sa.String(71), nullable=False),
        sa.Column("local_image_config_id", sa.String(71), nullable=False),
        sa.Column("oci_archive_sha256", sa.String(64), nullable=False),
        sa.Column("image_bytes", sa.BigInteger, nullable=False),
        sa.Column("build_id", sa.String(36)),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.ForeignKeyConstraint(
            ["recipe_revision_id"],
            ["catalog_document_revisions.id"],
            name="fk_runtime_image_authorizations_recipe_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["receipt_id"],
            ["runtime_image_receipts.id"],
            name="fk_runtime_image_authorizations_receipt",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["build_id"],
            ["recipe_builds.id"],
            name="fk_runtime_image_authorizations_build",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "recipe_revision_id",
            "receipt_id",
            name="uq_runtime_image_authorization_revision_receipt",
        ),
        sa.CheckConstraint(
            _lower_hex("original_content_digest", 64),
            name="ck_runtime_image_authorizations_original_digest",
        ),
        sa.CheckConstraint(
            _lower_hex("effective_execution_key", 64),
            name="ck_runtime_image_authorizations_execution_key",
        ),
        sa.CheckConstraint(
            _prefixed_digest("platform_manifest_digest"),
            name="ck_runtime_image_authorizations_platform_digest",
        ),
        sa.CheckConstraint(
            _prefixed_digest("local_image_config_id"),
            name="ck_runtime_image_authorizations_config_digest",
        ),
        sa.CheckConstraint(
            _lower_hex("oci_archive_sha256", 64),
            name="ck_runtime_image_authorizations_archive_digest",
        ),
        sa.CheckConstraint(
            "registry_manifest_digest IS NULL OR "
            f"({_prefixed_digest('registry_manifest_digest')})",
            name="ck_runtime_image_authorizations_registry_digest",
        ),
        sa.CheckConstraint(
            "image_bytes > 0",
            name="ck_runtime_image_authorizations_image_bytes",
        ),
        sa.CheckConstraint(
            "source IN ('published','controller-build')",
            name="ck_runtime_image_authorizations_source",
        ),
        sa.CheckConstraint(
            "state IN ('authorized','revoked')",
            name="ck_runtime_image_authorizations_state",
        ),
    )
    for column in (
        "recipe_revision_id",
        "receipt_id",
        "source",
        "original_content_digest",
        "effective_execution_key",
        "registry_manifest_digest",
        "platform_manifest_digest",
        "local_image_config_id",
        "oci_archive_sha256",
        "build_id",
        "state",
    ):
        op.create_index(
            f"ix_runtime_image_authorizations_{column}",
            "runtime_image_authorizations",
            [column],
        )
    op.create_index(
        "ix_runtime_image_authorizations_effective_identity",
        "runtime_image_authorizations",
        [
            "effective_execution_key",
            "platform_manifest_digest",
            "local_image_config_id",
            "oci_archive_sha256",
        ],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_runtime_image_authorizations_effective_identity",
        table_name="runtime_image_authorizations",
    )
    for column in (
        "state",
        "build_id",
        "oci_archive_sha256",
        "local_image_config_id",
        "platform_manifest_digest",
        "registry_manifest_digest",
        "effective_execution_key",
        "original_content_digest",
        "source",
        "receipt_id",
        "recipe_revision_id",
    ):
        op.drop_index(
            f"ix_runtime_image_authorizations_{column}",
            table_name="runtime_image_authorizations",
        )
    op.drop_table("runtime_image_authorizations")
