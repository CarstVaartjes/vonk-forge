"""Add the latest health observation lookup index."""

from alembic import op

revision = "0022_observation_latest_index"
down_revision = "0021_browser_authentication"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_observations_kind_node_observed",
        "observations",
        ["kind", "node_id", "observed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_observations_kind_node_observed",
        table_name="observations",
    )
