"""Create the current Controller schema from the canonical ORM metadata.

The Controller is a greenfield deployment. This revision deliberately has no
upgrade path from an older schema: an existing database is an operator action
to inspect and, when disposable, reset before starting the Controller.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect
from vonk_control.models import Base

revision = "0000_fresh_schema"
down_revision = None
branch_labels = None
depends_on = None

_ALEMBIC_VERSION_TABLE = "alembic_version"
_RESET_GUIDANCE = (
    "The Controller database is not compatible with the fresh schema baseline. "
    "No migration or automatic drop is performed. Inspect the database and, "
    "only when it is a disposable development database, reset that database "
    "explicitly before restarting the Controller."
)


def _existing_application_tables(bind: object) -> set[str]:
    return set(inspect(bind).get_table_names()) - {_ALEMBIC_VERSION_TABLE}


def upgrade() -> None:
    bind = op.get_bind()
    existing = _existing_application_tables(bind)
    if existing:
        tables = ", ".join(sorted(existing))
        raise RuntimeError(f"{_RESET_GUIDANCE} Existing tables: {tables}.")

    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    raise RuntimeError(
        "The fresh Controller schema has no downgrade path. "
        "Reset a disposable development database explicitly if removal is intended."
    )
