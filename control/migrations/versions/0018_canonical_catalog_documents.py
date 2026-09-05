"""Create the greenfield canonical model/recipe catalog projection."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_canonical_catalog_documents"
down_revision = "0017_dist_assignments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "catalog_documents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("publisher", sa.String(63), nullable=False),
        sa.Column("slug", sa.String(63), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('model','recipe')", name="ck_catalog_document_kind"),
        sa.CheckConstraint("publisher = lower(publisher) AND length(publisher) BETWEEN 2 AND 63", name="ck_catalog_document_publisher"),
        sa.CheckConstraint("slug = lower(slug) AND length(slug) BETWEEN 2 AND 63", name="ck_catalog_document_slug"),
        sa.CheckConstraint("length(title) BETWEEN 1 AND 120", name="ck_catalog_document_title"),
        sa.UniqueConstraint("kind", "publisher", "slug", name="uq_catalog_document_identity"),
        sa.UniqueConstraint("id", "kind", "publisher", "slug", name="uq_catalog_document_root_target"),
    )
    for column in ("kind", "publisher", "slug", "updated_at"):
        op.create_index(f"ix_catalog_documents_{column}", "catalog_documents", [column])
    op.create_table(
        "catalog_document_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("catalog_documents.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("publisher", sa.String(63), nullable=False),
        sa.Column("slug", sa.String(63), nullable=False),
        sa.Column("revision_number", sa.Integer, nullable=False),
        sa.Column("schema_version", sa.Integer, nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("document", sa.JSON, nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("artifact_key", sa.String(64)),
        sa.Column("execution_key", sa.String(64)),
        sa.Column("download_bytes", sa.BigInteger),
        sa.Column("installed_bytes", sa.BigInteger),
        sa.Column("projected", sa.JSON, nullable=False),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id", "kind", "publisher", "slug"],
            ["catalog_documents.id", "catalog_documents.kind", "catalog_documents.publisher", "catalog_documents.slug"],
            name="fk_catalog_document_revision_identity",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("revision_number >= 1", name="ck_catalog_document_revision_number"),
        sa.CheckConstraint("schema_version = 2", name="ck_catalog_document_revision_schema"),
        sa.CheckConstraint("state IN ('candidate','active','failed')", name="ck_catalog_document_revision_state"),
        sa.CheckConstraint("kind IN ('model','recipe')", name="ck_catalog_document_revision_kind"),
        sa.CheckConstraint("length(content_digest) = 64 AND content_digest = lower(content_digest)", name="ck_catalog_document_revision_digest"),
        sa.UniqueConstraint("document_id", "revision_number", name="uq_catalog_document_revision_number"),
        sa.UniqueConstraint("kind", "publisher", "slug", "content_digest", name="uq_catalog_document_revision_identity_digest"),
        sa.UniqueConstraint("id", "kind", "publisher", "slug", "content_digest", name="uq_catalog_document_revision_fk_target"),
        sa.UniqueConstraint("id", "kind", name="uq_catalog_document_revision_kind"),
    )
    for column in (
        "document_id",
        "kind",
        "publisher",
        "slug",
        "content_digest",
        "artifact_key",
        "execution_key",
    ):
        op.create_index(f"ix_catalog_document_revisions_{column}", "catalog_document_revisions", [column])
    op.create_table(
        "catalog_document_heads",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("publisher", sa.String(63), nullable=False),
        sa.Column("slug", sa.String(63), nullable=False),
        sa.Column("active_revision_id", sa.String(36), sa.ForeignKey("catalog_document_revisions.id", ondelete="RESTRICT")),
        sa.Column("candidate_revision_id", sa.String(36), sa.ForeignKey("catalog_document_revisions.id", ondelete="RESTRICT")),
        sa.Column("generation", sa.Integer, nullable=False),
        sa.CheckConstraint("kind IN ('model','recipe')", name="ck_catalog_document_head_kind"),
        sa.ForeignKeyConstraint(
            ["kind", "publisher", "slug"],
            ["catalog_documents.kind", "catalog_documents.publisher", "catalog_documents.slug"],
            name="fk_catalog_document_head_identity",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("kind", "publisher", "slug", name="uq_catalog_document_head_identity"),
    )
    op.create_index("ix_catalog_document_heads_active_revision_id", "catalog_document_heads", ["active_revision_id"])
    op.create_index("ix_catalog_document_heads_candidate_revision_id", "catalog_document_heads", ["candidate_revision_id"])
    op.create_table(
        "catalog_recipe_model_references",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("recipe_revision_id", sa.String(36), nullable=False),
        sa.Column("recipe_kind", sa.String(16), nullable=False),
        sa.Column("selection_id", sa.String(64), nullable=False),
        sa.Column("model_revision_id", sa.String(36), nullable=False),
        sa.Column("model_kind", sa.String(16), nullable=False),
        sa.Column("model_publisher", sa.String(63), nullable=False),
        sa.Column("model_slug", sa.String(63), nullable=False),
        sa.Column("model_content_digest", sa.String(64), nullable=False),
        sa.CheckConstraint("model_kind = 'model'", name="ck_catalog_recipe_model_kind"),
        sa.CheckConstraint("recipe_kind = 'recipe'", name="ck_catalog_recipe_revision_kind"),
        sa.ForeignKeyConstraint(
            ["recipe_revision_id", "recipe_kind"],
            ["catalog_document_revisions.id", "catalog_document_revisions.kind"],
            name="fk_catalog_recipe_revision_kind",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["model_revision_id", "model_kind", "model_publisher", "model_slug", "model_content_digest"],
            ["catalog_document_revisions.id", "catalog_document_revisions.kind", "catalog_document_revisions.publisher", "catalog_document_revisions.slug", "catalog_document_revisions.content_digest"],
            name="fk_catalog_recipe_model_exact_revision",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("recipe_revision_id", "selection_id", name="uq_catalog_recipe_model_selection"),
    )
    op.create_index("ix_catalog_recipe_model_references_recipe_revision_id", "catalog_recipe_model_references", ["recipe_revision_id"])
    op.create_index("ix_catalog_recipe_model_references_model_revision_id", "catalog_recipe_model_references", ["model_revision_id"])


def downgrade() -> None:
    op.drop_table("catalog_recipe_model_references")
    op.drop_table("catalog_document_heads")
    op.drop_table("catalog_document_revisions")
    op.drop_table("catalog_documents")
