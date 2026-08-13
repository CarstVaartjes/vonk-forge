"""Add a nullable Argon2 password verifier for browser authentication."""

import sqlalchemy as sa
from alembic import op

revision = "0021_browser_authentication"
down_revision = "0020_recipe_catalog_bridge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("password_verifier", sa.String(255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("password_verifier")
