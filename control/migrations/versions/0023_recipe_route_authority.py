"""Move recipe routes to a direct authority and retire graph storage."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0023_recipe_route_authority"
down_revision = "0022_current_telemetry_defaults"
branch_labels = None
depends_on = None

AUTHORITY_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "https://vonkforge.ai/local-recipes"))


def _drop_constraint(table: str, name: str) -> None:
    op.execute(sa.text(f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{name}"'))


def _sqlite_route_publications_source() -> sa.Table:
    metadata = sa.MetaData()
    return sa.Table(
        "route_publications",
        metadata,
        sa.Column(
            "reconciliation_id",
            sa.String(36),
            sa.ForeignKey("recipe_route_authorities.authority_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("generation", sa.BigInteger),
        sa.Column("plan_digest", sa.String(64), nullable=False),
        sa.Column("evidence_digest", sa.String(64)),
        sa.Column("route_digest", sa.String(64)),
        sa.Column("litellm_digest", sa.String(64)),
        sa.Column("bundle_digest", sa.String(64)),
        sa.Column("activation_marker", sa.JSON),
        sa.Column("activation_marker_digest", sa.String(64)),
        sa.Column("lease_issued_at", sa.DateTime(timezone=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "state IN ('withdrawal-pending', 'routes-withdrawn', "
            "'publication-pending', 'completed', 'failed')",
            name="ck_route_publications_state",
        ),
        sa.CheckConstraint(
            "generation IS NULL OR generation >= 0",
            name="ck_route_publications_generation",
        ),
        sa.CheckConstraint(
            "length(plan_digest) = 64",
            name="ck_route_publications_plan_digest_length",
        ),
        sa.CheckConstraint(
            "evidence_digest IS NULL OR length(evidence_digest) = 64",
            name="ck_route_publications_evidence_digest_length",
        ),
        sa.CheckConstraint(
            "route_digest IS NULL OR length(route_digest) = 64",
            name="ck_route_publications_route_digest_length",
        ),
        sa.CheckConstraint(
            "litellm_digest IS NULL OR length(litellm_digest) = 64",
            name="ck_route_publications_litellm_digest_length",
        ),
        sa.CheckConstraint(
            "bundle_digest IS NULL OR length(bundle_digest) = 64",
            name="ck_route_publications_bundle_digest_length",
        ),
        sa.CheckConstraint(
            "activation_marker_digest IS NULL OR length(activation_marker_digest) = 64",
            name="ck_route_publications_activation_marker_digest_length",
        ),
        sa.CheckConstraint(
            "lease_expires_at IS NULL OR "
            "(lease_issued_at IS NOT NULL AND lease_expires_at > lease_issued_at)",
            name="ck_route_publications_lease_window",
        ),
        sa.PrimaryKeyConstraint("reconciliation_id"),
    )


def _sqlite_route_publication_owner_source() -> sa.Table:
    metadata = sa.MetaData()
    return sa.Table(
        "route_publication_owner",
        metadata,
        sa.Column("singleton_id", sa.Integer, nullable=False),
        sa.Column(
            "reconciliation_id",
            sa.String(36),
            sa.ForeignKey("recipe_route_authorities.authority_id", ondelete="SET NULL"),
        ),
        sa.Column("owner_generation", sa.BigInteger, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "singleton_id = 1", name="ck_route_publication_owner_singleton"
        ),
        sa.CheckConstraint(
            "owner_generation >= 0", name="ck_route_publication_owner_generation"
        ),
        sa.PrimaryKeyConstraint("singleton_id"),
        sa.UniqueConstraint("reconciliation_id"),
    )


def _sqlite_route_tables() -> None:
    with op.batch_alter_table(
        "route_publications",
        recreate="always",
        copy_from=_sqlite_route_publications_source(),
    ) as batch:
        batch.alter_column(
            "reconciliation_id",
            new_column_name="authority_id",
            existing_type=sa.String(36),
            existing_nullable=False,
        )
    op.create_index(
        "ix_route_publications_generation",
        "route_publications",
        ["generation"],
        unique=True,
    )
    op.create_index("ix_route_publications_state", "route_publications", ["state"])
    with op.batch_alter_table(
        "route_publication_owner",
        recreate="always",
        copy_from=_sqlite_route_publication_owner_source(),
    ) as batch:
        batch.alter_column(
            "reconciliation_id",
            new_column_name="authority_id",
            existing_type=sa.String(36),
            existing_nullable=True,
        )


def _drop_graph_jobs(bind: sa.Connection) -> None:
    """Delete graph-owned jobs while retaining all current jobs and blobs."""

    bind.execute(sa.text("DELETE FROM reconciliation_operations"))
    bind.execute(
        sa.text(
            "DELETE FROM artifact_job_files WHERE artifact_job_id IN "
            "(SELECT id FROM artifact_jobs WHERE operation_id IN "
            "(SELECT id FROM jobs WHERE reconciliation_id IS NOT NULL))"
        )
    )
    bind.execute(
        sa.text(
            "DELETE FROM artifact_jobs WHERE operation_id IN "
            "(SELECT id FROM jobs WHERE reconciliation_id IS NOT NULL)"
        )
    )
    bind.execute(
        sa.text(
            "DELETE FROM job_log_entries WHERE job_id IN "
            "(SELECT id FROM jobs WHERE reconciliation_id IS NOT NULL)"
        )
    )
    bind.execute(
        sa.text(
            "DELETE FROM job_attempts WHERE job_id IN "
            "(SELECT id FROM jobs WHERE reconciliation_id IS NOT NULL)"
        )
    )
    bind.execute(sa.text("DELETE FROM jobs WHERE reconciliation_id IS NOT NULL"))


def _drop_job_pointer(bind: sa.Connection) -> None:
    if bind.dialect.name == "postgresql":
        _drop_constraint("jobs", "jobs_reconciliation_id_fkey")
        op.execute(sa.text("DROP INDEX IF EXISTS ix_jobs_reconciliation_id"))
        op.drop_column("jobs", "reconciliation_id")
        return
    op.drop_index("ix_jobs_reconciliation_id", table_name="jobs")
    with op.batch_alter_table("jobs", recreate="always") as batch:
        batch.drop_column("reconciliation_id")


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.now(UTC)
    op.create_table(
        "recipe_route_authorities",
        sa.Column("authority_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("authority_id"),
    )
    bind.execute(
        sa.text(
            "INSERT INTO recipe_route_authorities "
            "(authority_id, created_at, updated_at) VALUES (:id, :now, :now)"
        ),
        {"id": AUTHORITY_ID, "now": now},
    )

    # Existing activation markers use the retired wire identity.  Start route
    # publication state cleanly; the next current publisher creates a marker
    # carrying authority_id and a matching digest.
    bind.execute(sa.text("DELETE FROM route_publications"))
    bind.execute(sa.text("DELETE FROM route_publication_owner"))
    _drop_graph_jobs(bind)

    if bind.dialect.name == "postgresql":
        _drop_constraint(
            "route_publications", "route_publications_reconciliation_id_fkey"
        )
        _drop_constraint(
            "route_publication_owner", "route_publication_owner_reconciliation_id_fkey"
        )
        op.execute(
            sa.text(
                "ALTER TABLE route_publications "
                "RENAME COLUMN reconciliation_id TO authority_id"
            )
        )
        op.execute(
            sa.text(
                "ALTER TABLE route_publication_owner "
                "RENAME COLUMN reconciliation_id TO authority_id"
            )
        )
        op.create_foreign_key(
            "fk_route_publications_authority",
            "route_publications",
            "recipe_route_authorities",
            ["authority_id"],
            ["authority_id"],
            ondelete="CASCADE",
        )
        op.create_foreign_key(
            "fk_route_publication_owner_authority",
            "route_publication_owner",
            "recipe_route_authorities",
            ["authority_id"],
            ["authority_id"],
            ondelete="SET NULL",
        )
    else:
        _sqlite_route_tables()

    _drop_job_pointer(bind)
    op.drop_table("reconciliation_operations")
    op.drop_table("reconciliation_cancellations")
    op.drop_table("reconciliation_completion_generation")
    op.drop_table("reconciliations")


def downgrade() -> None:
    raise RuntimeError("The route-authority migration is forward-only")
