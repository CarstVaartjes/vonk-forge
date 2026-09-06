"""Persist explicit Fleet profile membership scope."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_fleet_profile_scope"
down_revision = "0013_repeatable_install_plans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("fleet_profiles") as batch:
        batch.add_column(
            sa.Column(
                "scope",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("fleet_profiles") as batch:
        batch.drop_column("scope")
