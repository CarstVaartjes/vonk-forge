"""Persist bounded content-addressed artifact-producing recipe jobs."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_artifact_jobs"
down_revision = "0003_agent_reenrollment_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "artifact_job_blobs",
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(sha256) = 64 AND sha256 = lower(sha256) AND "
            "length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0",
            name="ck_artifact_job_blobs_digest",
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_artifact_job_blobs_size"),
        sa.PrimaryKeyConstraint("sha256"),
        sa.UniqueConstraint("storage_key"),
    )
    op.create_table(
        "artifact_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("operation_id", sa.String(length=36), nullable=True),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("interface", sa.String(length=24), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("output_limits", sa.JSON(), nullable=False),
        sa.Column("compiled_contract", sa.JSON(), nullable=False),
        sa.Column("contract_sha256", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("input_manifest", sa.JSON(), nullable=False),
        sa.Column("input_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "input_total_bytes", sa.BigInteger(), server_default="0", nullable=False
        ),
        sa.Column("output_manifest_sha256", sa.String(length=64), nullable=True),
        sa.Column("output_manifest", sa.JSON(), nullable=True),
        sa.Column("result_evidence", sa.JSON(), nullable=True),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("actor", sa.String(length=200), nullable=False),
        sa.Column("status_reason", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "interface IN ('audio-job','video-job','image-job','mesh-job','artifact-job')",
            name="ck_artifact_jobs_interface",
        ),
        sa.CheckConstraint(
            "state IN ('draft','ready','queued','running','cancelling','waiting-for-operator','succeeded','failed','cancelled')",
            name="ck_artifact_jobs_state",
        ),
        sa.CheckConstraint(
            "input_total_bytes >= 0 AND timeout_seconds BETWEEN 1 AND 3600",
            name="ck_artifact_jobs_limits",
        ),
        sa.CheckConstraint(
            "length(contract_sha256) = 64 AND contract_sha256 = lower(contract_sha256) AND "
            "length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(contract_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0",
            name="ck_artifact_jobs_contract",
        ),
        sa.ForeignKeyConstraint(["operation_id"], ["jobs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["recipe_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id"),
    )
    op.create_index("ix_artifact_jobs_run_id", "artifact_jobs", ["run_id"])
    op.create_index(
        "ix_artifact_jobs_operation_id", "artifact_jobs", ["operation_id"], unique=True
    )
    op.create_index("ix_artifact_jobs_state", "artifact_jobs", ["state"])
    op.create_table(
        "artifact_job_files",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("artifact_job_id", sa.String(length=36), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("slot", sa.String(length=32), nullable=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("media_type", sa.String(length=129), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("blob_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "direction IN ('input','output')", name="ck_artifact_job_files_direction"
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_artifact_job_files_size"),
        sa.ForeignKeyConstraint(
            ["artifact_job_id"], ["artifact_jobs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["blob_sha256"], ["artifact_job_blobs.sha256"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "artifact_job_id", "direction", "name", name="uq_artifact_job_file_name"
        ),
    )
    op.create_index(
        "ix_artifact_job_files_artifact_job_id",
        "artifact_job_files",
        ["artifact_job_id"],
    )
    op.create_index(
        "ix_artifact_job_files_blob_sha256",
        "artifact_job_files",
        ["blob_sha256"],
    )


def downgrade() -> None:
    op.drop_table("artifact_job_files")
    op.drop_table("artifact_jobs")
    op.drop_table("artifact_job_blobs")
