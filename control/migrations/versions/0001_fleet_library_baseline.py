"""Create the fresh Fleet and Library control-plane baseline."""

from __future__ import annotations

from alembic import op
from vonk_control.models import Base

revision = "0001_fleet_library_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind)


def downgrade() -> None:
    bind = op.get_bind()
    for table in reversed(Base.metadata.sorted_tables):
        table.drop(bind, checkfirst=True)
