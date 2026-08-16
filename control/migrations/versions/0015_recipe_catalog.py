"""Add the authoritative local recipe catalog."""

import sqlalchemy as sa
from alembic import op

revision = "0015_recipe_catalog"
down_revision = "0014_package_action_plans"
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
        "package_families",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("provider_kind", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.Integer, nullable=False),
        sa.Column("definition", sa.JSON, nullable=False),
        sa.Column("builtin", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("schema_version >= 1", name="ck_package_families_schema"),
        sa.CheckConstraint("length(id) BETWEEN 1 AND 128", name="ck_package_families_id"),
    )
    op.create_index("ix_package_families_provider_kind", "package_families", ["provider_kind"])
    op.create_table(
        "recipe_source_bundles",
        sa.Column("sha256", sa.String(64), primary_key=True),
        sa.Column("media_type", sa.String(96), nullable=False),
        sa.Column("archive_bytes", sa.BigInteger, nullable=False),
        sa.Column("total_bytes", sa.BigInteger, nullable=False),
        sa.Column("file_count", sa.Integer, nullable=False),
        sa.Column("storage_key", sa.String(255), nullable=False, unique=True),
        sa.Column("manifest", sa.JSON, nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(_lower_hex("sha256", 64), name="ck_recipe_source_bundle_digest"),
        sa.CheckConstraint(
            "archive_bytes > 0 AND total_bytes >= 0 AND file_count >= 1",
            name="ck_recipe_source_bundle_sizes",
        ),
    )
    op.create_table(
        "local_recipes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("slug", sa.String(128), nullable=False, unique=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("source_kind", sa.String(16), nullable=False),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("source_kind IN ('local','workload_run','global','recipe_library')", name="ck_local_recipes_source_kind"),
        sa.CheckConstraint("slug = lower(slug) AND length(slug) BETWEEN 2 AND 128", name="ck_local_recipes_slug"),
    )
    op.create_index("ix_local_recipes_source_kind", "local_recipes", ["source_kind"])
    op.create_table(
        "local_recipe_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("recipe_id", sa.String(36), sa.ForeignKey("local_recipes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision_number", sa.Integer, nullable=False),
        sa.Column("lifecycle", sa.String(16), nullable=False),
        sa.Column("schema_version", sa.Integer, nullable=False),
        sa.Column("document", sa.JSON, nullable=False),
        sa.Column("content_sha256", sa.String(64)),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("recipe_id", "revision_number", name="uq_local_recipe_revision_number"),
        sa.UniqueConstraint("recipe_id", "content_sha256", name="uq_local_recipe_revision_content"),
        sa.CheckConstraint("revision_number >= 1", name="ck_local_recipe_revisions_number"),
        sa.CheckConstraint("schema_version >= 1", name="ck_local_recipe_revisions_schema"),
        sa.CheckConstraint("lifecycle IN ('draft','blocked','resolved','deprecated')", name="ck_local_recipe_revisions_lifecycle"),
        sa.CheckConstraint("lifecycle != 'resolved' OR content_sha256 IS NOT NULL", name="ck_local_recipe_revisions_resolved_digest"),
        sa.CheckConstraint(_nullable_lower_hex("content_sha256", 64), name="ck_local_recipe_revisions_content_digest"),
    )
    op.create_index("ix_local_recipe_revisions_recipe_id", "local_recipe_revisions", ["recipe_id"])
    op.create_index("ix_local_recipe_revisions_lifecycle", "local_recipe_revisions", ["lifecycle"])
    op.create_index("ix_local_recipe_revisions_content_sha256", "local_recipe_revisions", ["content_sha256"])
    op.create_table(
        "recipe_imports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("recipe_id", sa.String(36), sa.ForeignKey("local_recipes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_kind", sa.String(16), nullable=False),
        sa.Column("source_reference", sa.Text, nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("redacted_source", sa.JSON, nullable=False),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("source_kind IN ('local','workload_run','global','recipe_library')", name="ck_recipe_imports_source_kind"),
        sa.CheckConstraint(_lower_hex("source_sha256", 64), name="ck_recipe_imports_source_digest"),
        sa.UniqueConstraint("source_kind", "source_sha256", name="uq_recipe_import_source"),
    )
    op.create_index("ix_recipe_imports_recipe_id", "recipe_imports", ["recipe_id"])
    op.create_index("ix_recipe_imports_source_sha256", "recipe_imports", ["source_sha256"])
    op.create_table(
        "recipe_import_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("import_id", sa.String(36), sa.ForeignKey("recipe_imports.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_path", sa.Text, nullable=False),
        sa.Column("disposition", sa.String(32), nullable=False),
        sa.Column("destination_path", sa.Text),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("detail", sa.Text, nullable=False),
        sa.Column("blocking", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.CheckConstraint(
            "disposition IN ('imported','incorporated','resolved','transformed','resolution_required',"
            "'overlay_required','unsupported_blocking','dropped_redundant')",
            name="ck_recipe_import_items_disposition",
        ),
    )
    op.create_index("ix_recipe_import_items_import_id", "recipe_import_items", ["import_id"])
    op.create_index("ix_recipe_import_items_disposition", "recipe_import_items", ["disposition"])
    op.create_table(
        "recipe_global_links",
        sa.Column("recipe_id", sa.String(36), sa.ForeignKey("local_recipes.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("global_recipe_id", sa.String(36), nullable=False),
        sa.Column("global_publisher", sa.String(63), nullable=False),
        sa.Column("global_slug", sa.String(63), nullable=False),
        sa.Column("global_revision", sa.Integer, nullable=False),
        sa.Column("global_content_sha256", sa.String(64), nullable=False),
        sa.Column("sync_state", sa.String(24), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("global_revision >= 1", name="ck_recipe_global_links_revision"),
        sa.CheckConstraint(_lower_hex("global_content_sha256", 64), name="ck_recipe_global_links_digest"),
        sa.CheckConstraint("sync_state IN ('current','local-ahead','remote-ahead','unavailable')", name="ck_recipe_global_links_state"),
    )
    op.create_index("ix_recipe_global_links_global_recipe_id", "recipe_global_links", ["global_recipe_id"])
    op.create_table(
        "recipe_builds",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("recipe_revision_id", sa.String(36), sa.ForeignKey("local_recipe_revisions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("builder_node_id", sa.String(36), sa.ForeignKey("agent_nodes.node_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source_bundle_sha256", sa.String(64), nullable=False),
        sa.Column("build_input_sha256", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("policy_report", sa.JSON, nullable=False),
        sa.Column("plan", sa.JSON, nullable=False),
        sa.Column("image_digest", sa.String(71)),
        sa.Column("oci_layout_sha256", sa.String(64)),
        sa.Column("image_bytes", sa.BigInteger),
        sa.Column("error", sa.String(512)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("recipe_revision_id", "builder_node_id", "build_input_sha256", name="uq_recipe_build_input_builder"),
        sa.CheckConstraint("state IN ('planned','building','succeeded','failed')", name="ck_recipe_builds_state"),
        sa.CheckConstraint(_lower_hex("source_bundle_sha256", 64), name="ck_recipe_builds_source_digest"),
        sa.CheckConstraint(_lower_hex("build_input_sha256", 64), name="ck_recipe_builds_input_digest"),
        sa.CheckConstraint("image_digest IS NULL OR (length(image_digest) = 71 AND substr(image_digest, 1, 7) = 'sha256:')", name="ck_recipe_builds_image_digest"),
        sa.CheckConstraint("oci_layout_sha256 IS NULL OR length(oci_layout_sha256) = 64", name="ck_recipe_builds_layout_digest"),
        sa.CheckConstraint("image_bytes IS NULL OR image_bytes > 0", name="ck_recipe_builds_image_size"),
    )
    op.create_index("ix_recipe_builds_recipe_revision_id", "recipe_builds", ["recipe_revision_id"])
    op.create_index("ix_recipe_builds_builder_node_id", "recipe_builds", ["builder_node_id"])
    op.create_index("ix_recipe_builds_state", "recipe_builds", ["state"])
    op.create_table(
        "cluster_mappings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("recipe_revision_id", sa.String(36), sa.ForeignKey("local_recipe_revisions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("profile_name", sa.String(64), nullable=False),
        sa.Column("generation", sa.Integer, nullable=False),
        sa.Column("node_count", sa.Integer, nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("parameters", sa.JSON, nullable=False),
        sa.Column("placement_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("endpoint_owner_node_id", sa.String(36), sa.ForeignKey("agent_nodes.node_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("generation >= 1", name="ck_cluster_mappings_generation"),
        sa.CheckConstraint("node_count >= 1", name="ck_cluster_mappings_node_count"),
        sa.CheckConstraint("state IN ('planned','ready','stale')", name="ck_cluster_mappings_state"),
        sa.CheckConstraint(_lower_hex("placement_digest", 64), name="ck_cluster_mappings_placement_digest"),
    )
    op.create_index("ix_cluster_mappings_recipe_revision_id", "cluster_mappings", ["recipe_revision_id"])
    op.create_index("ix_cluster_mappings_state", "cluster_mappings", ["state"])
    op.create_table(
        "cluster_mapping_nodes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("mapping_id", sa.String(36), sa.ForeignKey("cluster_mappings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("node_id", sa.String(36), sa.ForeignKey("agent_nodes.node_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("rank", sa.Integer, nullable=False),
        sa.Column("role", sa.String(64), nullable=False),
        sa.Column("endpoint_owner", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("mapping_id", "node_id", name="uq_cluster_mapping_node"),
        sa.UniqueConstraint("mapping_id", "rank", name="uq_cluster_mapping_rank"),
        sa.CheckConstraint("rank >= 0", name="ck_cluster_mapping_nodes_rank"),
        sa.CheckConstraint("length(role) BETWEEN 1 AND 64", name="ck_cluster_mapping_nodes_role"),
    )
    op.create_index("ix_cluster_mapping_nodes_mapping_id", "cluster_mapping_nodes", ["mapping_id"])
    op.create_index("ix_cluster_mapping_nodes_node_id", "cluster_mapping_nodes", ["node_id"])


def downgrade() -> None:
    for table in (
        "cluster_mapping_nodes",
        "cluster_mappings",
        "recipe_builds",
        "recipe_global_links",
        "recipe_import_items",
        "recipe_imports",
        "local_recipe_revisions",
        "local_recipes",
        "recipe_source_bundles",
        "package_families",
    ):
        op.drop_table(table)
