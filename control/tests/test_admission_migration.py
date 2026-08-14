from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect


def config(url: str) -> Config:
    root = Path(__file__).resolve().parents[1]; value = Config(root / "alembic.ini")
    value.set_main_option("script_location", str(root / "migrations")); value.set_main_option("sqlalchemy.url", url); return value


def test_admission_state_is_linear_head() -> None:
    script = ScriptDirectory.from_config(config("sqlite://"))
    assert script.get_heads() == ["0022_observation_latest_index"]
    assert script.get_revision("0017_admission_and_run_state").down_revision == "0016_recipe_deployment_authority"


def test_admission_tables_upgrade_and_downgrade(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path/'admission.sqlite'}"; command.upgrade(config(url), "head"); engine = create_engine(url)
    tables = {"node_inventory_snapshots", "node_artifacts", "resource_reservations", "recipe_installations", "installation_nodes", "recipe_runs", "run_nodes"}
    assert tables <= set(inspect(engine).get_table_names())
    assert {"recipe_revision_id", "plan_digest", "state"} <= {column["name"] for column in inspect(engine).get_columns("recipe_installations")}
    assert {"reserved_memory_bytes", "observed_memory_bytes", "rank", "role"} <= {column["name"] for column in inspect(engine).get_columns("run_nodes")}
    command.downgrade(config(url), "0016_recipe_deployment_authority")
    assert not tables & set(inspect(engine).get_table_names())
