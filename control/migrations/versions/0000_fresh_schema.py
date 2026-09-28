"""Create missing Controller tables from the canonical ORM metadata.

Startup reconciles an existing schema transactionally after this baseline has
run. Keeping table creation here lets a database without an Alembic revision
join the same convergence path as one that already records this baseline.
"""

from __future__ import annotations

from alembic import op
from vonk_control.models import Base

revision = "0000_fresh_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    raise RuntimeError(
        "The fresh Controller schema has no downgrade path. "
        "Reset a disposable development database explicitly if removal is intended."
    )
