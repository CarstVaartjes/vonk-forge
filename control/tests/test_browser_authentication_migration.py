from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import String, create_engine, inspect, text
from vonk_control.models import User


def config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[1]
    value = Config(root / "alembic.ini")
    value.set_main_option("script_location", str(root / "migrations"))
    value.set_main_option("sqlalchemy.url", database_url)
    return value


def test_browser_authentication_follows_catalog_bridge() -> None:
    script = ScriptDirectory.from_config(config("sqlite://"))
    assert script.get_revision("0021_browser_authentication").down_revision == (
        "0020_recipe_catalog_bridge"
    )


def test_password_verifier_migration_preserves_users_and_downgrades_only_its_column(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'browser-authentication.sqlite'}"
    migration_config = config(url)
    command.upgrade(migration_config, "0020_recipe_catalog_bridge")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, subject, role, disabled_at) "
                "VALUES ('00000000-0000-4000-8000-000000000001', 'admin', 'admin', NULL)"
            )
        )

    command.upgrade(migration_config, "head")
    columns = {column["name"]: column for column in inspect(engine).get_columns("users")}
    assert columns["password_verifier"]["type"].length == 255
    assert columns["password_verifier"]["nullable"] is True
    with engine.begin() as connection:
        assert connection.execute(
            text("SELECT password_verifier FROM users WHERE subject = 'admin'")
        ).scalar_one() is None
        verifier = "$argon2id$v=19$m=65536,t=3,p=1$c2FsdC0xNi1ieXRlcyE$dmVyaWZpZXItMzItYnl0ZXM"
        connection.execute(
            text("UPDATE users SET password_verifier = :verifier WHERE subject = 'admin'"),
            {"verifier": verifier},
        )
        assert connection.execute(
            text("SELECT password_verifier FROM users WHERE subject = 'admin'")
        ).scalar_one() == verifier

    command.downgrade(migration_config, "0020_recipe_catalog_bridge")
    assert {column["name"] for column in inspect(engine).get_columns("users")} == set(
        columns
    ) - {
        "password_verifier"
    }


def test_user_metadata_has_a_nullable_bounded_password_verifier() -> None:
    column = User.__table__.c.password_verifier
    assert isinstance(column.type, String)
    assert column.type.length == 255
    assert column.nullable is True
