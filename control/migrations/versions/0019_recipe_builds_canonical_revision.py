"""Bind recipe builds to the canonical recipe document revision."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# Keep the Alembic revision within alembic_version's VARCHAR(32).
revision = "0019_recipe_builds_revision_fk"
down_revision = "0018_canonical_catalog_documents"
branch_labels = None
depends_on = None

_CANONICAL_TARGET = "catalog_document_revisions.id"
_LEGACY_TARGET = "local_recipe_revisions.id"
_CANONICAL_CONSTRAINT = "fk_recipe_builds_canonical_recipe_revision"
_LEGACY_CONSTRAINT = "fk_recipe_builds_legacy_recipe_revision"


def _replace_sqlite_foreign_key(target: str, constraint_name: str) -> None:
    """Recreate the SQLite table while retaining rows, indexes, and constraints.

    SQLite has no ALTER CONSTRAINT operation.  Reflection keeps this migration
    independent of the fixed baseline's unnamed foreign-key constraint while
    Alembic's batch copy preserves the table's contents and indexes.
    """

    bind = op.get_bind()
    metadata = sa.MetaData()
    table = sa.Table("recipe_builds", metadata, autoload_with=bind)
    for constraint in list(table.constraints):
        if not isinstance(constraint, sa.ForeignKeyConstraint):
            continue
        if [column.name for column in constraint.columns] == ["recipe_revision_id"]:
            table.constraints.remove(constraint)
    table.append_constraint(
        sa.ForeignKeyConstraint(
            ["recipe_revision_id"],
            [target],
            name=constraint_name,
            ondelete="RESTRICT",
        )
    )
    with op.batch_alter_table(
        "recipe_builds", recreate="always", copy_from=table
    ):
        pass


def _replace_foreign_key(target: str, constraint_name: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        _replace_sqlite_foreign_key(target, constraint_name)
        return

    inspector = sa.inspect(bind)
    current = next(
        (
            foreign_key
            for foreign_key in inspector.get_foreign_keys("recipe_builds")
            if foreign_key.get("constrained_columns") == ["recipe_revision_id"]
        ),
        None,
    )
    if current is None:
        raise RuntimeError(
            "recipe_builds.recipe_revision_id foreign key is missing; "
            "refusing to guess at the catalog migration"
        )
    referred_table = current.get("referred_table")
    referred_column = (current.get("referred_columns") or [None])[0]
    if f"{referred_table}.{referred_column}" == target:
        return
    if not current.get("name"):
        raise RuntimeError(
            "recipe_builds.recipe_revision_id foreign key has no name; "
            "refusing an unbounded live constraint replacement"
        )
    op.drop_constraint(current["name"], "recipe_builds", type_="foreignkey")
    referred_table, referred_column = target.split(".", 1)
    op.create_foreign_key(
        constraint_name,
        "recipe_builds",
        referred_table,
        ["recipe_revision_id"],
        [referred_column],
        ondelete="RESTRICT",
    )


def upgrade() -> None:
    _replace_foreign_key(_CANONICAL_TARGET, _CANONICAL_CONSTRAINT)


def downgrade() -> None:
    _replace_foreign_key(_LEGACY_TARGET, _LEGACY_CONSTRAINT)
