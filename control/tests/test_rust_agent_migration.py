from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_rust_agent_migration_is_the_linear_database_head() -> None:
    root = Path(__file__).resolve().parents[1]
    scripts = ScriptDirectory.from_config(Config(root / "alembic.ini"))

    assert scripts.get_current_head() == "0021_browser_authentication"
    revision = scripts.get_revision("0019_rust_agent_migration")
    assert revision is not None
    assert revision.down_revision == "0018_agent_inventory_runtime"
