from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import JSON, bindparam, create_engine, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import AgentIdentity, AgentSource
from vonk_control.models import AgentCertificate, AgentNode
from vonk_control.presence import AgentPresenceService, ManagementAddressPolicy


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@pytest.fixture(scope="module")
def postgres_database() -> Iterator[str]:
    if shutil.which("docker") is None:
        pytest.skip("Docker is required for PostgreSQL migration tests")
    try:
        container = subprocess.check_output(
            [
                "docker",
                "run",
                "--rm",
                "-d",
                "-e",
                "POSTGRES_PASSWORD=postgres",
                "-p",
                "127.0.0.1::5432",
                "postgres:16",
            ],
            text=True,
        ).strip()
    except subprocess.CalledProcessError as error:
        pytest.skip(f"disposable PostgreSQL is unavailable: {error}")
    try:
        port = subprocess.check_output(
            [
                "docker",
                "inspect",
                "-f",
                '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}',
                container,
            ],
            text=True,
        ).strip()
        database = (
            "postgresql+psycopg://postgres:postgres@127.0.0.1:"
            f"{port}/postgres"
        )
        engine = create_engine(database)
        for _ in range(100):
            try:
                with engine.connect():
                    break
            except (OSError, SQLAlchemyError):
                time.sleep(0.1)
        else:
            pytest.skip("disposable PostgreSQL did not become ready")
        engine.dispose()
        yield database
    finally:
        subprocess.run(
            ["docker", "stop", container], check=False, capture_output=True
        )


def _seed_0008(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO reconciliations "
                "(id,base_commit,status,summary,created_at) VALUES "
                "('legacy-reconciliation',"
                "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','planned','{}',"
                "'2026-08-05 00:00:00+00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO jobs "
                "(id,request_id,kind,state,actor,base_commit,targets,"
                "payload_digest,payload,current_attempt,created_at,updated_at) "
                "VALUES ('legacy-job','11111111-1111-1111-1111-111111111111',"
                "'reconcile','queued','admin',"
                "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','[]',"
                f"'{'b' * 64}','{{}}',0,"
                "'2026-08-05 00:00:00+00','2026-08-05 00:00:00+00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO agent_nodes (node_id,state,capabilities) VALUES "
                "('spk_0123456789abcdef0123456789abcdef','active','[]')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO agent_operations "
                "(id,parent_job_id,node_id,kind,payload_digest,payload,"
                "base_commit,state,current_attempt,created_at,updated_at) VALUES "
                "('legacy-agent-operation','legacy-job',"
                "'spk_0123456789abcdef0123456789abcdef','node.probe',"
                f"'{'c' * 64}','{{}}',"
                "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','queued',0,"
                "'2026-08-05 00:00:00+00','2026-08-05 00:00:00+00')"
            )
        )


def _assert_legacy_rows(engine: Engine) -> None:
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT status FROM reconciliations WHERE id='legacy-reconciliation'")
        ).scalar_one() == "planned"
        assert connection.execute(
            text("SELECT state FROM jobs WHERE id='legacy-job'")
        ).scalar_one() == "queued"
        assert connection.execute(
            text(
                "SELECT state FROM agent_nodes "
                "WHERE node_id='spk_0123456789abcdef0123456789abcdef'"
            )
        ).scalar_one() == "active"
        assert connection.execute(
            text(
                "SELECT state FROM agent_operations "
                "WHERE id='legacy-agent-operation'"
            )
        ).scalar_one() == "queued"


def _assert_execution_cycle(database: str) -> None:
    config = _config(database)
    engine = create_engine(database)
    command.upgrade(config, "0008_resolved_plan")
    _seed_0008(engine)

    command.upgrade(config, "0009_reconciliation_execution")
    database_inspector = inspect(engine)
    assert {
        "agent_presence",
        "reconciliation_cancellations",
        "reconciliation_operations",
        "route_publication_owner",
        "route_publications",
    } <= set(database_inspector.get_table_names())
    assert "reconciliation_id" in {
        column["name"] for column in database_inspector.get_columns("jobs")
    }

    operation_columns = {
        column["name"]
        for column in database_inspector.get_columns("reconciliation_operations")
    }
    assert operation_columns == {
        "id",
        "reconciliation_id",
        "graph_operation_id",
        "role",
        "agent_operation_id",
        "expected_payload_digest",
        "state",
        "result_digest",
        "evidence_digest",
        "accepted_at",
        "compensated_graph_operation_id",
    }
    publication_columns = {
        column["name"]
        for column in database_inspector.get_columns("route_publications")
    }
    assert publication_columns == {
        "reconciliation_id",
        "state",
        "generation",
        "plan_digest",
        "evidence_digest",
        "route_digest",
        "litellm_digest",
        "bundle_digest",
        "activation_marker",
        "activation_marker_digest",
        "lease_issued_at",
        "lease_expires_at",
    }
    cancellation_columns = {
        column["name"]
        for column in database_inspector.get_columns("reconciliation_cancellations")
    }
    assert cancellation_columns == {
        "reconciliation_id",
        "state",
        "reason",
        "actor",
        "request_id",
        "requested_at",
        "updated_at",
    }
    owner_columns = {
        column["name"]
        for column in database_inspector.get_columns("route_publication_owner")
    }
    assert owner_columns == {
        "singleton_id",
        "reconciliation_id",
        "owner_generation",
        "updated_at",
    }
    presence_columns = {
        column["name"]
        for column in database_inspector.get_columns("agent_presence")
    }
    assert presence_columns == {
        "node_id",
        "certificate_serial",
        "certificate_fingerprint",
        "management_address",
        "observed_at",
    }

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO agent_certificates "
                "(serial,node_id,not_before,not_after,fingerprint,state,generation) "
                "VALUES ('presence-serial',"
                "'spk_0123456789abcdef0123456789abcdef',"
                "'2026-08-05 00:00:00+00','2026-08-06 00:00:00+00',"
                "'presence-fingerprint','active',1)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO agent_presence "
                "(node_id,certificate_serial,certificate_fingerprint,"
                "management_address,observed_at) VALUES "
                "('spk_0123456789abcdef0123456789abcdef','presence-serial',"
                "'presence-fingerprint','10.0.0.42','2026-08-05 00:01:00+00')"
            )
        )
        connection.execute(
            text(
                "UPDATE jobs SET reconciliation_id='legacy-reconciliation' "
                "WHERE id='legacy-job'"
            )
        )
        connection.execute(
            text(
                "INSERT INTO reconciliation_operations "
                "(id,reconciliation_id,graph_operation_id,role,agent_operation_id,"
                "expected_payload_digest,state,result_digest,evidence_digest,"
                "accepted_at,compensated_graph_operation_id) VALUES "
                "('execution-1','legacy-reconciliation','model:probe','primary',"
                "'legacy-agent-operation',"
                f"'{'d' * 64}','accepted','{'e' * 64}','{'f' * 64}',"
                "'2026-08-05 00:01:00+00',NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO route_publications "
                "(reconciliation_id,state,generation,plan_digest,evidence_digest,"
                "route_digest,litellm_digest,bundle_digest,"
                "activation_marker,activation_marker_digest,"
                "lease_issued_at,lease_expires_at) VALUES "
                "('legacy-reconciliation','completed',7,"
                f"'{'1' * 64}','{'2' * 64}','{'3' * 64}','{'4' * 64}',"
                f"'{'5' * 64}',"
                ":activation_marker,"
                f"'{'6' * 64}',"
                "'2026-08-05 00:01:00+00','2026-08-05 00:03:30+00')"
            ).bindparams(bindparam("activation_marker", type_=JSON)),
            {
                "activation_marker": {
                    "schema_version": 1,
                    "state": "published",
                }
            },
        )
        connection.execute(
            text(
                "INSERT INTO reconciliation_cancellations "
                "(reconciliation_id,state,reason,actor,request_id,"
                "requested_at,updated_at) VALUES "
                "('legacy-reconciliation','requested','operator request',"
                "'operator','22222222-2222-4222-8222-222222222222',"
                "'2026-08-05 00:01:00+00','2026-08-05 00:01:00+00')"
            )
        )
        connection.execute(
            text(
                "UPDATE route_publication_owner SET "
                "reconciliation_id='legacy-reconciliation',owner_generation=7,"
                "updated_at='2026-08-05 00:01:00+00' WHERE singleton_id=1"
            )
        )
    _assert_legacy_rows(engine)

    command.downgrade(config, "0008_resolved_plan")
    database_inspector = inspect(engine)
    assert not {
        "agent_presence",
        "reconciliation_cancellations",
        "reconciliation_operations",
        "route_publication_owner",
        "route_publications",
    } & set(database_inspector.get_table_names())
    assert "reconciliation_id" not in {
        column["name"] for column in database_inspector.get_columns("jobs")
    }
    _assert_legacy_rows(engine)

    command.upgrade(config, "0009_reconciliation_execution")
    _assert_legacy_rows(engine)
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM reconciliation_operations")
        ).scalar_one() == 0
        assert connection.execute(
            text("SELECT count(*) FROM route_publications")
        ).scalar_one() == 0
        assert connection.execute(
            text("SELECT count(*) FROM reconciliation_cancellations")
        ).scalar_one() == 0
        assert connection.execute(
            text("SELECT count(*) FROM agent_presence")
        ).scalar_one() == 0
        assert connection.execute(
            text(
                "SELECT reconciliation_id,owner_generation "
                "FROM route_publication_owner WHERE singleton_id=1"
            )
        ).one() == (None, 0)
        assert connection.execute(
            text("SELECT reconciliation_id FROM jobs WHERE id='legacy-job'")
        ).scalar_one() is None
    engine.dispose()


def test_latest_migration_is_the_sole_linear_head() -> None:
    config = _config("sqlite://")
    scripts = ScriptDirectory.from_config(config)
    heads = scripts.get_heads()

    assert heads == ["0021_browser_authentication"]
    revision = ScriptDirectory.from_config(config).get_revision(heads[0])
    assert revision is not None
    assert revision.down_revision == "0020_recipe_catalog_bridge"
    assert [item.revision for item in reversed(tuple(scripts.walk_revisions()))] == [
        "0001_operational_state",
        "0002_agent_operations",
        "0003_retry_disposition",
        "0004_agent_enrollment",
        "0005_certificate_rotation",
        "0006_reconciliation_graph",
        "0007_issued_revocations",
        "0008_resolved_plan",
        "0009_reconciliation_execution",
        "0010_agent_runtime_identity",
        "0011_update_rollouts",
            "0012_control_process_heartbeats",
            "0013_workload_packages",
            "0014_package_action_plans",
            "0015_recipe_catalog",
            "0016_recipe_deployment_authority",
            "0017_admission_and_run_state",
            "0018_agent_inventory_runtime",
            "0019_rust_agent_migration",
            "0020_recipe_catalog_bridge",
            "0021_browser_authentication",
        ]


def test_execution_models_expose_durable_links_and_bounded_fields() -> None:
    from vonk_control import models

    assert hasattr(models, "ReconciliationOperation")
    assert hasattr(models, "ReconciliationCancellation")
    assert hasattr(models, "RoutePublication")
    assert hasattr(models, "RoutePublicationOwner")
    assert hasattr(models, "AgentPresence")
    assert models.Job.__table__.c.reconciliation_id.unique
    assert models.Job.__table__.c.reconciliation_id.foreign_keys

    operation = models.ReconciliationOperation.__table__
    assert operation.c.reconciliation_id.foreign_keys
    assert operation.c.agent_operation_id.foreign_keys
    assert operation.c.agent_operation_id.unique
    assert operation.c.graph_operation_id.type.length == 128
    assert operation.c.role.type.length == 16
    assert operation.c.state.type.length == 32
    assert any(
        constraint.columns.keys()
        == ["reconciliation_id", "graph_operation_id", "role"]
        for constraint in operation.constraints
    )

    publication = models.RoutePublication.__table__
    assert publication.c.reconciliation_id.primary_key
    assert publication.c.reconciliation_id.foreign_keys
    assert publication.c.state.type.length == 32
    assert publication.c.generation.unique
    assert "activation_marker" in publication.c

    cancellation = models.ReconciliationCancellation.__table__
    assert cancellation.c.reconciliation_id.primary_key
    assert cancellation.c.reconciliation_id.foreign_keys
    assert cancellation.c.request_id.unique
    assert cancellation.c.state.type.length == 32
    assert cancellation.c.actor.type.length == 200

    owner = models.RoutePublicationOwner.__table__
    assert owner.c.singleton_id.primary_key
    assert owner.c.reconciliation_id.foreign_keys
    assert owner.c.reconciliation_id.unique
    assert owner.c.owner_generation.type.python_type is int

    presence = models.AgentPresence.__table__
    assert presence.c.node_id.primary_key
    assert presence.c.node_id.foreign_keys
    assert presence.c.certificate_serial.foreign_keys
    assert presence.c.management_address.type.length == 45
    assert presence.c.observed_at.index


def test_sqlite_rejects_execution_states_outside_closed_sets(
    tmp_path: Path,
) -> None:
    database = f"sqlite:///{tmp_path / 'bounded-states.sqlite'}"
    config = _config(database)
    engine = create_engine(database)
    command.upgrade(config, "0008_resolved_plan")
    _seed_0008(engine)
    command.upgrade(config, "0009_reconciliation_execution")

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO reconciliation_operations "
                "(id,reconciliation_id,graph_operation_id,role,"
                "expected_payload_digest,state) VALUES "
                "('bad-execution-state','legacy-reconciliation','model:probe',"
                f"'primary','{'d' * 64}','arbitrary')"
            )
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO route_publications "
                "(reconciliation_id,state,plan_digest) VALUES "
                f"('legacy-reconciliation','arbitrary','{'1' * 64}')"
            )
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO reconciliation_cancellations "
                "(reconciliation_id,state,reason,actor,request_id,"
                "requested_at,updated_at) VALUES "
                "('legacy-reconciliation','arbitrary','operator request',"
                "'operator','22222222-2222-4222-8222-222222222222',"
                "'2026-08-05 00:01:00+00','2026-08-05 00:01:00+00')"
            )
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE route_publication_owner SET singleton_id=2 "
                "WHERE singleton_id=1"
            )
        )
    engine.dispose()


def test_sqlite_0008_0009_preservation_cycle(tmp_path: Path) -> None:
    _assert_execution_cycle(f"sqlite:///{tmp_path / 'execution.sqlite'}")


def test_postgresql_0008_0009_preservation_cycle(
    postgres_database: str,
) -> None:
    _assert_execution_cycle(postgres_database)


def test_postgresql_presence_reuses_an_existing_node_lock(
    postgres_database: str,
) -> None:
    # AgentNode gains runtime and migration columns after the original
    # execution migration; use the current schema while exercising the
    # presence lock semantics.
    command.upgrade(_config(postgres_database), "head")
    engine = create_engine(
        postgres_database,
        connect_args={"options": "-c lock_timeout=1000ms"},
    )
    sessions = sessionmaker(engine, expire_on_commit=False)
    node_id = "spk_" + "9" * 32
    now = datetime(2026, 8, 5, 12, tzinfo=UTC)
    with sessions.begin() as session:
        session.add(AgentNode(node_id=node_id, state="active", capabilities=[]))
        session.flush()
        session.add(
            AgentCertificate(
                serial="presence-lock-serial",
                node_id=node_id,
                fingerprint="presence-lock-fingerprint",
                state="active",
                generation=1,
                not_before=now - timedelta(minutes=1),
                not_after=now + timedelta(hours=1),
            )
        )
    service = AgentPresenceService(
        sessions,
        ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=lambda: now,
    )
    service.observe(
        AgentSource(
            AgentIdentity(
                node_id,
                "presence-lock-serial",
                "presence-lock-fingerprint",
                True,
            ),
            "10.0.0.42",
        )
    )

    with sessions.begin() as session:
        locked = session.scalar(
            select(AgentNode)
            .where(AgentNode.node_id == node_id)
            .with_for_update(of=AgentNode)
        )
        assert locked is not None

        observation = service.latest_in_session(
            session,
            node_id,
            maximum_age_seconds=60,
        )

        assert observation.address == "10.0.0.42"
        updated = service.observe_in_session(
            session,
            AgentSource(
                AgentIdentity(
                    node_id,
                    "presence-lock-serial",
                    "presence-lock-fingerprint",
                    True,
                ),
                "10.0.0.43",
            ),
        )
        assert updated.address == "10.0.0.43"
    engine.dispose()
