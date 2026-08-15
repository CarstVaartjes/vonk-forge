"""Contract tests for the W11 workload package operational projection."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

HEX = "a" * 64
COMMIT = "b" * 40
NODE = "spk_0123456789abcdef0123456789abcdef"


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _upgrade(database: str, revision: str = "head") -> None:
    command.upgrade(_config(database), revision)


def _downgrade(database: str, revision: str) -> None:
    command.downgrade(_config(database), revision)


def _insert_node(connection) -> None:
    connection.execute(
        text(
            "INSERT INTO agent_nodes (node_id,state,capabilities) "
            "VALUES (:node,'active','[]')"
        ),
        {"node": NODE},
    )


def _insert_candidate(connection, *, candidate_id: str = "candidate-1") -> None:
    connection.execute(
        text(
            "INSERT INTO package_candidates "
            "(id,family_id,upstream_identity_digest,metadata_digest,"
            " upstream_version,source_provider,source_reference,state,discovered_by,"
            " first_seen_at,last_seen_at,created_at,updated_at) "
            "VALUES (:id,'synthetic-stack',:identity,:metadata,'1.0.0','git',"
            " 'https://example.invalid/repository','discovered','scheduler',"
            " '2026-08-06 00:00:00','2026-08-06 00:00:00',"
            " '2026-08-06 00:00:00','2026-08-06 00:00:00')"
        ),
        {"id": candidate_id, "identity": HEX, "metadata": "c" * 64},
    )


def test_workload_migrations_follow_recipe_deployment_authority() -> None:
    root = Path(__file__).resolve().parents[1]
    script = ScriptDirectory.from_config(_config("sqlite://"))
    revision = script.get_revision("0017_admission_and_run_state")
    assert revision.down_revision == "0016_recipe_deployment_authority"
    assert root.joinpath("migrations/versions/0013_workload_packages.py").exists()
    assert root.joinpath("migrations/versions/0014_package_action_plans.py").exists()


def test_workload_tables_upgrade_and_downgrade_as_one_boundary(tmp_path: Path) -> None:
    database = f"sqlite:///{tmp_path / 'workloads.sqlite'}"
    _upgrade(database)
    engine = create_engine(database)
    names = set(inspect(engine).get_table_names())
    package_tables = {
        "package_candidates",
        "package_resolutions",
        "package_validation_runs",
        "package_rollouts",
        "package_rollout_nodes",
        "package_observations",
    }
    assert package_tables <= names

    _downgrade(database, "0012_control_process_heartbeats")
    assert not package_tables & set(inspect(engine).get_table_names())


def test_candidate_and_resolution_identities_are_retry_safe(tmp_path: Path) -> None:
    database = f"sqlite:///{tmp_path / 'identity.sqlite'}"
    _upgrade(database)
    engine = create_engine(database)
    with engine.begin() as connection:
        _insert_candidate(connection)
        with pytest.raises(IntegrityError):
            _insert_candidate(connection, candidate_id="candidate-2")

        connection.execute(
            text(
                "INSERT INTO package_resolutions "
                "(id,candidate_id,resolver_id,resolver_schema_version,state,"
                "resolved_by,created_at,updated_at) VALUES "
                "('resolution-1','candidate-1','resolver-v1',1,'pending','worker',"
                "'2026-08-06 00:00:00','2026-08-06 00:00:00')"
            )
        )
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO package_resolutions "
                    "(id,candidate_id,resolver_id,resolver_schema_version,state,"
                    "resolved_by,created_at,updated_at) VALUES "
                    "('resolution-2','candidate-1','resolver-v1',1,'pending','worker',"
                    "'2026-08-06 00:00:00','2026-08-06 00:00:00')"
                )
            )


def test_resolution_and_rollout_require_immutable_digests(tmp_path: Path) -> None:
    database = f"sqlite:///{tmp_path / 'constraints.sqlite'}"
    _upgrade(database)
    engine = create_engine(database)
    with engine.begin() as connection:
        _insert_candidate(connection)
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO package_resolutions "
                    "(id,candidate_id,resolver_id,resolver_schema_version,state,"
                    "release_digest,resolved_by,created_at,updated_at) VALUES "
                    "('resolution-bad','candidate-1','resolver-v1',1,'resolved',"
                    "'not-a-digest','worker','2026-08-06 00:00:00',"
                    "'2026-08-06 00:00:00')"
                )
            )

        # A rollout cannot represent a promoted release without a full Git
        # commit and a workload-TUF target digest.
        values = {
            "id": "rollout-1",
            "deployment_digest": HEX,
            "release_digest": HEX,
            "policy_digest": HEX,
            "tuf_target_digest": HEX,
            "fleet_digest": HEX,
            "topology_digest": HEX,
            "plan_digest": "d" * 64,
            "authority_digest": "e" * 64,
        }
        connection.execute(
            text(
                "INSERT INTO package_rollouts "
                "(id,deployment_id,deployment_digest,release_digest,base_commit,"
                "policy_digest,tuf_target_digest,fleet_digest,topology_digest,"
                "plan_digest,authority_digest,state,actor,created_at,updated_at) VALUES "
                "(:id,'synthetic-deployment',:deployment_digest,:release_digest,"
                ":base_commit,:policy_digest,:tuf_target_digest,:fleet_digest,"
                ":topology_digest,:plan_digest,:authority_digest,'planned','admin',"
                "'2026-08-06 00:00:00','2026-08-06 00:00:00')"
            ),
            {**values, "base_commit": COMMIT},
        )
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO package_rollouts "
                    "(id,deployment_id,deployment_digest,release_digest,base_commit,"
                    "policy_digest,tuf_target_digest,fleet_digest,topology_digest,"
                    "plan_digest,authority_digest,state,actor,created_at,updated_at) VALUES "
                    "('rollout-2','synthetic-deployment',:deployment_digest,"
                    ":release_digest,'short',:policy_digest,:tuf_target_digest,"
                    ":fleet_digest,:topology_digest,'e' || :plan_digest,"
                    ":authority_digest,'planned','admin','2026-08-06 00:00:00',"
                    "'2026-08-06 00:00:00')"
                ),
                values,
            )


def test_package_state_has_no_payload_or_secret_storage_columns(tmp_path: Path) -> None:
    database = f"sqlite:///{tmp_path / 'shape.sqlite'}"
    _upgrade(database)
    engine = create_engine(database)
    package_tables = {
        "package_candidates",
        "package_resolutions",
        "package_validation_runs",
        "package_rollouts",
        "package_rollout_nodes",
        "package_observations",
    }
    forbidden_exact = {"lock_bytes", "release_lock", "source_credentials", "signed_url"}
    for table in package_tables:
        columns = {column["name"] for column in inspect(engine).get_columns(table)}
        assert not columns & forbidden_exact
        assert "secret" not in " ".join(columns).lower()


def test_candidate_delete_cascades_only_derived_resolution_and_validation_state(
    tmp_path: Path,
) -> None:
    database = f"sqlite:///{tmp_path / 'cascade.sqlite'}"
    _upgrade(database)
    engine = create_engine(database)
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
        _insert_candidate(connection)
        connection.execute(
            text(
                "INSERT INTO package_resolutions "
                "(id,candidate_id,resolver_id,resolver_schema_version,state,"
                "resolved_by,created_at,updated_at) VALUES "
                "('resolution-1','candidate-1','resolver-v1',1,'pending','worker',"
                "'2026-08-06 00:00:00','2026-08-06 00:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO package_validation_runs "
                "(id,candidate_id,resolution_id,validation_kind,release_digest,"
                "policy_digest,fleet_digest,state,actor,created_at,updated_at) VALUES "
                "('validation-1','candidate-1','resolution-1','artifact',:digest,"
                ":digest,:digest,'planned','worker','2026-08-06 00:00:00',"
                "'2026-08-06 00:00:00')"
            ),
            {"digest": HEX},
        )
        connection.execute(
            text("DELETE FROM package_candidates WHERE id='candidate-1'")
        )
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM package_resolutions")
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM package_validation_runs")
            ).scalar_one()
            == 0
        )
