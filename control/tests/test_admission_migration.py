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
    assert script.get_heads() == ["0023_node_telemetry"]
    assert (
        script.get_revision("0023_node_telemetry").down_revision
        == "0022_observation_latest_index"
    )
    assert script.get_revision("0017_admission_and_run_state").down_revision == "0016_recipe_deployment_authority"


def test_admission_tables_upgrade_and_downgrade(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path/'admission.sqlite'}"; command.upgrade(config(url), "head"); engine = create_engine(url)
    tables = {"node_inventory_snapshots", "node_artifacts", "resource_reservations", "recipe_installations", "installation_nodes", "recipe_runs", "run_nodes", "node_telemetry_samples", "node_telemetry_latest"}
    assert tables <= set(inspect(engine).get_table_names())
    assert {"recipe_revision_id", "plan_digest", "state"} <= {column["name"] for column in inspect(engine).get_columns("recipe_installations")}
    assert {"reserved_memory_bytes", "observed_memory_bytes", "rank", "role"} <= {column["name"] for column in inspect(engine).get_columns("run_nodes")}
    assert {
        "node_id",
        "boot_id",
        "sequence",
        "observed_at",
        "received_at",
        "cpu_utilization_percent",
        "memory_available_bytes",
        "gpu_utilization_percent",
        "details",
    } <= {
        column["name"]
        for column in inspect(engine).get_columns("node_telemetry_samples")
    }
    assert {"node_id", "sample_id"} == {
        column["name"]
        for column in inspect(engine).get_columns("node_telemetry_latest")
    }
    sample_uniques = {
        (constraint["name"], tuple(constraint["column_names"]))
        for constraint in inspect(engine).get_unique_constraints(
            "node_telemetry_samples"
        )
    }
    assert (
        "uq_telemetry_node_sample",
        ("node_id", "id"),
    ) in sample_uniques
    latest_foreign_keys = inspect(engine).get_foreign_keys("node_telemetry_latest")
    assert any(
        foreign_key["constrained_columns"] == ["node_id", "sample_id"]
        and foreign_key["referred_table"] == "node_telemetry_samples"
        and foreign_key["referred_columns"] == ["node_id", "id"]
        for foreign_key in latest_foreign_keys
    )
    checks = {
        constraint["name"]: constraint["sqltext"]
        for constraint in inspect(engine).get_check_constraints(
            "node_telemetry_samples"
        )
    }
    capacity_limit = "17592186044416"
    assert all(
        capacity_limit in checks[name]
        for name in (
            "ck_telemetry_memory",
            "ck_telemetry_disk",
            "ck_telemetry_gpu_memory",
        )
    )
    assert "1000000000000000" in checks["ck_telemetry_physical_metrics"]
    command.downgrade(config(url), "0016_recipe_deployment_authority")
    assert not tables & set(inspect(engine).get_table_names())
