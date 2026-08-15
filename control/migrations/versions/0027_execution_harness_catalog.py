"""Create the fresh v1 execution-harness catalog schema."""

import sqlalchemy as sa
from alembic import op

revision = "0027_execution_harness_catalog"
down_revision = "0026_telemetry_maintenance_state"
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


def _nullable_lower_hex(column: str, length: int) -> str:
    return f"{column} IS NULL OR ({_lower_hex(column, length)})"


def upgrade() -> None:
    op.create_table(
        "catalog_entities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("publisher", sa.String(63), nullable=False),
        sa.Column("slug", sa.String(63), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "kind", "publisher", "slug", name="uq_catalog_entities_identity"
        ),
        sa.CheckConstraint(
            "kind IN ('model-group','model','model-version','execution-harness',"
            "'runtime-distribution','patch-bundle')",
            name="ck_catalog_entities_kind",
        ),
        sa.CheckConstraint(
            "publisher = lower(publisher) AND length(publisher) BETWEEN 2 AND 63",
            name="ck_catalog_entities_publisher",
        ),
        sa.CheckConstraint(
            "slug = lower(slug) AND length(slug) BETWEEN 2 AND 63",
            name="ck_catalog_entities_slug",
        ),
        sa.CheckConstraint(
            "length(title) BETWEEN 1 AND 120", name="ck_catalog_entities_title"
        ),
    )
    op.create_index("ix_catalog_entities_kind", "catalog_entities", ["kind"])
    op.create_index("ix_catalog_entities_publisher", "catalog_entities", ["publisher"])
    op.create_index("ix_catalog_entities_slug", "catalog_entities", ["slug"])
    op.create_index(
        "ix_catalog_entities_updated_at", "catalog_entities", ["updated_at"]
    )

    op.create_table(
        "catalog_entity_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "entity_id",
            sa.String(36),
            sa.ForeignKey("catalog_entities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("revision_number", sa.Integer, nullable=False),
        sa.Column("lifecycle", sa.String(16), nullable=False),
        sa.Column("schema_version", sa.Integer, nullable=False),
        sa.Column("document", sa.JSON, nullable=False),
        sa.Column("content_sha256", sa.String(64)),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "entity_id", "revision_number", name="uq_catalog_entity_revision_number"
        ),
        sa.UniqueConstraint(
            "entity_id", "content_sha256", name="uq_catalog_entity_revision_content"
        ),
        sa.CheckConstraint(
            "revision_number >= 1", name="ck_catalog_entity_revisions_number"
        ),
        sa.CheckConstraint(
            "schema_version >= 1", name="ck_catalog_entity_revisions_schema"
        ),
        sa.CheckConstraint(
            "lifecycle IN ('draft','blocked','resolved','deprecated')",
            name="ck_catalog_entity_revisions_lifecycle",
        ),
        sa.CheckConstraint(
            "lifecycle != 'resolved' OR content_sha256 IS NOT NULL",
            name="ck_catalog_entity_revisions_resolved_digest",
        ),
        sa.CheckConstraint(
            _nullable_lower_hex("content_sha256", 64),
            name="ck_catalog_entity_revisions_content_digest",
        ),
    )
    op.create_index(
        "ix_catalog_entity_revisions_entity_id",
        "catalog_entity_revisions",
        ["entity_id"],
    )
    op.create_index(
        "ix_catalog_entity_revisions_lifecycle",
        "catalog_entity_revisions",
        ["lifecycle"],
    )
    op.create_index(
        "ix_catalog_entity_revisions_content_sha256",
        "catalog_entity_revisions",
        ["content_sha256"],
    )

    if op.get_bind().dialect.name == "postgresql":
        op.alter_column(
            "cluster_mappings",
            "profile_name",
            new_column_name="topology_name",
            existing_type=sa.String(64),
            existing_nullable=False,
        )
    else:
        with op.batch_alter_table(
            "cluster_mappings",
            recreate="always",
            partial_reordering=[
                (
                    "id",
                    "recipe_revision_id",
                    "topology_name",
                    "generation",
                    "node_count",
                    "state",
                    "parameters",
                    "placement_digest",
                    "endpoint_owner_node_id",
                    "created_by",
                    "created_at",
                    "updated_at",
                )
            ],
        ) as batch:
            batch.add_column(sa.Column("topology_name", sa.String(64), nullable=False))
            batch.drop_column("profile_name")


def downgrade() -> None:
    raise RuntimeError(
        "0027_execution_harness_catalog is a fresh pre-production cutover and "
        "does not support downgrade"
    )
