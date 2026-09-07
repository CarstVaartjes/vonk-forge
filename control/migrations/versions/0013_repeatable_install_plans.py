"""Allow repeated acceptance of the same immutable install plan."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_repeatable_install_plans"
down_revision = "0012_recipe_run_generation"
branch_labels = None
depends_on = None


def _plan_digest_constraint() -> str | None:
    constraints = sa.inspect(op.get_bind()).get_unique_constraints(
        "recipe_installations"
    )
    for constraint in constraints:
        if constraint.get("column_names") == ["plan_digest"]:
            name = constraint.get("name")
            return name if isinstance(name, str) else None
    raise RuntimeError("recipe_installations.plan_digest unique constraint is missing")


def upgrade() -> None:
    name = _plan_digest_constraint()
    if name is None:
        # SQLite does not retain the baseline constraint's implicit name.
        # A batch naming convention gives Alembic a stable reflected name while
        # it rebuilds the table.
        with op.batch_alter_table(
            "recipe_installations",
            naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"},
        ) as batch:
            batch.drop_constraint(
                "uq_recipe_installations_plan_digest", type_="unique"
            )
        return
    op.drop_constraint(name, "recipe_installations", type_="unique")


def downgrade() -> None:
    # Fresh baseline foreign keys point at canonical revisions; asking SQLite
    # to resolve the historical local revision target during a batch rebuild
    # would fail because that table is intentionally absent.
    with op.batch_alter_table(
        "recipe_installations", reflect_kwargs={"resolve_fks": False}
    ) as batch:
        batch.create_unique_constraint(
            "uq_recipe_installations_plan_digest", ["plan_digest"]
        )
