"""Persist exact model-version ownership for recipe installations.

Revision ID: 0011_recipe_model_identity
Revises: 0010_managed_recipe_catalog_sync
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_recipe_model_identity"
down_revision = "0010_managed_recipe_catalog_sync"
branch_labels = None
depends_on = None

_TABLE = "recipe_installations"
_CONSTRAINT = "ck_recipe_installations_model_version_sha256"
_EXPRESSION = (
    "model_version_sha256 IS NULL OR "
    "(length(model_version_sha256) = 64 AND "
    "model_version_sha256 = lower(model_version_sha256) AND "
    "length(replace(replace(replace(replace(replace(replace(replace(replace("
    "replace(replace(replace(replace(replace(replace(replace(replace("
    "model_version_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), "
    "'5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), "
    "'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0)"
)


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        with op.batch_alter_table(_TABLE, recreate="always") as batch:
            batch.add_column(
                sa.Column("model_version_sha256", sa.String(length=64), nullable=True)
            )
            batch.create_check_constraint(_CONSTRAINT, _EXPRESSION)
    else:
        op.add_column(
            _TABLE,
            sa.Column("model_version_sha256", sa.String(length=64), nullable=True),
        )
        op.create_check_constraint(_CONSTRAINT, _TABLE, _EXPRESSION)
    op.create_index(
        "ix_recipe_installations_model_version_sha256",
        _TABLE,
        ["model_version_sha256"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_recipe_installations_model_version_sha256",
        table_name=_TABLE,
    )
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        with op.batch_alter_table(_TABLE, recreate="always") as batch:
            batch.drop_constraint(_CONSTRAINT, type_="check")
            batch.drop_column("model_version_sha256")
        return
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.drop_column(_TABLE, "model_version_sha256")
