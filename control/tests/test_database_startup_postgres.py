from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pytest import MonkeyPatch
from sqlalchemy.engine import Connection, Engine
from vonk_agent_protocol.inventory import InventoryRequest
from vonk_agent_protocol.state_machines import NodeIdentityState
from vonk_control.db import initialize_database
from vonk_control.db import startup as startup_owner
from vonk_control.models import NodeInventorySnapshot
from vonk_control.stored_json import ContractJSON


def test_concurrent_fresh_startup_migrates_once(postgres_engine: Engine) -> None:
    with postgres_engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    database_url = postgres_engine.url.render_as_string(hide_password=False)
    config_path = Path(__file__).resolve().parents[1] / "alembic.ini"
    expected_migration_head = ScriptDirectory.from_config(
        Config(str(config_path))
    ).get_current_head()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(
            pool.map(
                lambda _: initialize_database(
                    database_url,
                    config_path=config_path,
                ),
                range(2),
            )
        )
    with postgres_engine.connect() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one()
            == expected_migration_head
        )


def test_bookkeeping_adoption_failure_preserves_current_schema(
    postgres_engine: Engine,
    monkeypatch: MonkeyPatch,
) -> None:
    from vonk_control import db

    def broken_adoption(connection: Connection) -> None:
        # PostgreSQL aborts the current transaction after this fault; only a
        # real savepoint prevents it from undoing schema reconciliation.
        connection.exec_driver_sql("SELECT * FROM missing_adoption_bookkeeping")

    monkeypatch.setattr(startup_owner, "adopt_legacy_rows", broken_adoption)
    initialize_database(postgres_engine.url.render_as_string(hide_password=False))
    with postgres_engine.connect() as connection:
        db.verify_schema_is_current(connection)
        assert connection.exec_driver_sql("SELECT 1").scalar_one() == 1


def test_contact_move_rolls_back_on_interruption_and_restarts(
    postgres_engine: Engine, monkeypatch: MonkeyPatch
) -> None:
    """Catches dropping the old owner before a durable copy or skipping replay."""
    import pytest
    from sqlalchemy import event, insert, inspect
    from vonk_control.db import node_facts
    from vonk_control.models import AgentNode, Base

    Base.metadata.create_all(postgres_engine)
    with postgres_engine.begin() as connection:
        connection.execute(
            insert(AgentNode),
            [
                {"node_id": "legacy", "state": NodeIdentityState.ACTIVE},
                {"node_id": "empty", "state": NodeIdentityState.ACTIVE},
            ],
        )
        connection.exec_driver_sql(
            "ALTER TABLE agent_nodes DROP COLUMN management_address"
        )
        connection.exec_driver_sql(
            "ALTER TABLE agent_nodes DROP COLUMN management_observed_at"
        )
        connection.exec_driver_sql("""CREATE TABLE agent_presence (
            node_id varchar(36) PRIMARY KEY, certificate_serial varchar(128),
            certificate_fingerprint varchar(128), management_address varchar(45),
            observed_at timestamptz)""")
        connection.exec_driver_sql("""INSERT INTO agent_presence VALUES
            ('legacy', 'old-certificate', 'derived-not-copied', '10.0.0.42',
             '2026-10-22T12:00:00Z')""")

    original_move = node_facts.move_contact_facts

    def interrupted_move(connection: Connection) -> None:
        def interrupt(_conn, _cursor, statement, _parameters, _context, _many):
            if statement == "DROP TABLE agent_presence":
                raise InterruptedError(
                    "Controller stopped after copy, before retirement"
                )

        event.listen(connection, "before_cursor_execute", interrupt)
        try:
            original_move(connection)
        finally:
            event.remove(connection, "before_cursor_execute", interrupt)

    url = postgres_engine.url.render_as_string(hide_password=False)
    with monkeypatch.context() as interruption:
        interruption.setattr(node_facts, "move_contact_facts", interrupted_move)
        with pytest.raises(InterruptedError):
            initialize_database(url)
    with postgres_engine.connect() as connection:
        assert "agent_presence" in inspect(connection).get_table_names()
        assert "management_address" not in {
            column["name"] for column in inspect(connection).get_columns("agent_nodes")
        }
    initialize_database(url)
    initialize_database(url)
    with postgres_engine.connect() as connection:
        assert "agent_presence" not in inspect(connection).get_table_names()
        assert connection.exec_driver_sql("""SELECT management_address,
            contact_certificate_serial FROM agent_nodes WHERE node_id='legacy'
        """).one() == ("10.0.0.42", "old-certificate")
        assert connection.exec_driver_sql("""SELECT management_address,
            management_observed_at FROM agent_nodes WHERE node_id='empty'
        """).one() == (None, None)


def test_runtime_items_migration_is_atomic_and_does_not_invent_capacity(
    postgres_engine: Engine, monkeypatch: MonkeyPatch
) -> None:
    """Catches a lost identity/hostname on restart, or a fabricated capacity row."""
    import pytest
    from sqlalchemy import event, insert, inspect, select, text
    from sqlalchemy.orm import sessionmaker
    from vonk_agent_protocol.claims import AgentRuntimeIdentity
    from vonk_control.db import node_facts
    from vonk_control.inventory_items import inventory_items
    from vonk_control.models import (
        AgentNode,
        AgentNodeProfile,
        Base,
        NodeInventoryItemsSnapshot,
        NodeInventorySnapshot,
    )

    Base.metadata.create_all(postgres_engine)
    with postgres_engine.begin() as connection:
        connection.execute(
            insert(AgentNode),
            [
                {"node_id": "reported", "state": NodeIdentityState.ACTIVE},
                {"node_id": "empty", "state": NodeIdentityState.ACTIVE},
            ],
        )
        connection.execute(
            insert(AgentNodeProfile),
            {
                "node_id": "reported",
                "display_name": "Spark",
                "labels": {},
            },
        )
        for column, sql_type in (
            ("architecture", "varchar(16)"),
            ("semantic_version", "varchar(32)"),
            ("build_digest", "varchar(71)"),
            ("binary_digest", "varchar(64)"),
            ("preflight_fingerprint", "varchar(64)"),
        ):
            connection.exec_driver_sql(
                f"ALTER TABLE agent_nodes ADD COLUMN {column} {sql_type}"
            )
        connection.exec_driver_sql(
            "ALTER TABLE agent_node_profiles ADD COLUMN hostname varchar(255)"
        )
        connection.exec_driver_sql(
            "UPDATE agent_node_profiles SET hostname='spark.internal'"
        )
        identity = AgentRuntimeIdentity(
            architecture="linux-arm64",
            semantic_version="1.2.3",
            build_digest="sha256:" + "b" * 64,
            binary_digest="c" * 64,
        )
        connection.execute(
            text("""UPDATE agent_nodes SET
            architecture=:architecture, semantic_version=:semantic_version,
            build_digest=:build_digest, binary_digest=:binary_digest
            WHERE node_id='reported'"""),
            identity.model_dump(exclude={"package_activation"}),
        )

    original = node_facts.move_runtime_facts

    def interrupted(connection: Connection) -> None:
        def stop(_conn, _cursor, statement, _parameters, _context, _many):
            if statement == "ALTER TABLE agent_nodes DROP COLUMN architecture":
                raise InterruptedError(
                    "Controller stopped before retiring source columns"
                )

        event.listen(connection, "before_cursor_execute", stop)
        try:
            original(connection)
        finally:
            event.remove(connection, "before_cursor_execute", stop)

    url = postgres_engine.url.render_as_string(hide_password=False)
    with monkeypatch.context() as interruption:
        interruption.setattr(node_facts, "move_runtime_facts", interrupted)
        with pytest.raises(InterruptedError):
            initialize_database(url)
    with postgres_engine.connect() as connection:
        assert "architecture" in {
            column["name"] for column in inspect(connection).get_columns("agent_nodes")
        }
        assert connection.scalar(select(NodeInventoryItemsSnapshot.id)) is None
    initialize_database(url)
    initialize_database(url)
    with sessionmaker(postgres_engine)() as session:
        node = session.get(AgentNode, "reported")
        items = inventory_items(node)
        assert items.runtime_identity == identity
        assert items.hostname == "spark.internal"
        assert inventory_items(session.get(AgentNode, "empty")).runtime_identity is None
        assert session.scalar(select(NodeInventorySnapshot.id)) is None
    with postgres_engine.connect() as connection:
        assert "architecture" not in {
            column["name"] for column in inspect(connection).get_columns("agent_nodes")
        }
        assert "hostname" not in {
            column["name"]
            for column in inspect(connection).get_columns("agent_node_profiles")
        }


def test_partial_runtime_evidence_waits_for_claim_then_retires_columns(
    postgres_engine: Engine,
) -> None:
    """Catches discarding incomplete evidence or keeping it as a permanent gate."""
    from datetime import UTC, datetime

    from sqlalchemy import insert, inspect
    from sqlalchemy.orm import sessionmaker
    from vonk_agent_protocol.claims import AgentRuntimeIdentity
    from vonk_control.inventory_items import inventory_items, record_runtime
    from vonk_control.models import AgentNode, Base

    Base.metadata.create_all(postgres_engine)
    with postgres_engine.begin() as connection:
        connection.execute(
            insert(AgentNode), {"node_id": "partial", "state": NodeIdentityState.ACTIVE}
        )
        for name in (
            "architecture",
            "semantic_version",
            "build_digest",
            "binary_digest",
            "preflight_fingerprint",
        ):
            connection.exec_driver_sql(
                f"ALTER TABLE agent_nodes ADD COLUMN {name} varchar(128)"
            )
        connection.exec_driver_sql(
            "ALTER TABLE agent_node_profiles ADD COLUMN hostname varchar(255)"
        )
        connection.exec_driver_sql("UPDATE agent_nodes SET architecture='linux-arm64'")
    url = postgres_engine.url.render_as_string(hide_password=False)
    initialize_database(url)
    initialize_database(url)
    with postgres_engine.connect() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT architecture FROM agent_nodes"
            ).scalar_one()
            == "linux-arm64"
        )
    sessions = sessionmaker(postgres_engine)
    with sessions.begin() as session:
        node = session.get(AgentNode, "partial")
        assert node is not None
        assert inventory_items(node).runtime_identity is None
        record_runtime(
            node,
            AgentRuntimeIdentity(
                architecture="linux-arm64",
                semantic_version="1.2.3",
                build_digest="sha256:" + "b" * 64,
                binary_digest="c" * 64,
            ),
            None,
            "reported.internal",
            datetime.now(UTC),
        )
    initialize_database(url)
    with postgres_engine.connect() as connection:
        assert "architecture" not in {
            column["name"] for column in inspect(connection).get_columns("agent_nodes")
        }
    with sessions() as session:
        assert (
            inventory_items(session.get(AgentNode, "partial")).hostname
            == "reported.internal"
        )


@pytest.mark.parametrize("with_receipt", [False, True])
def test_artifact_move_preserves_runtime_items_and_restarts_after_interruption(
    postgres_engine: Engine,
    monkeypatch: MonkeyPatch,
    with_receipt: bool,
) -> None:
    """Catches lost availability on a restart and clobbering another inventory item."""
    from datetime import UTC, datetime

    from sqlalchemy import event, inspect, text
    from sqlalchemy.orm import sessionmaker
    from vonk_agent_protocol import ModelFileState
    from vonk_agent_protocol.claims import AgentRuntimeIdentity
    from vonk_control.db import node_facts
    from vonk_control.inventory_items import inventory_items, record_runtime
    from vonk_control.models import AgentNode, Base

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine)
    at = datetime(2026, 10, 22, 12, tzinfo=UTC)
    with sessions.begin() as session:
        node = AgentNode(node_id="artifact-owner", state=NodeIdentityState.ACTIVE)
        session.add(node)
        record_runtime(
            node,
            AgentRuntimeIdentity(
                architecture="linux-arm64",
                semantic_version="1.2.3",
                build_digest="sha256:" + "b" * 64,
                binary_digest="c" * 64,
            ),
            "d" * 64,
            "preserved.internal",
            at,
        )
    with postgres_engine.begin() as connection:
        connection.exec_driver_sql("""CREATE TABLE node_artifacts (
            id varchar(36) PRIMARY KEY, node_id varchar(36), kind varchar(24),
            digest varchar(64), source text, size_bytes bigint, state varchar(24),
            ref_count integer, verified_at timestamptz, updated_at timestamptz)""")
        if with_receipt:
            connection.execute(
                text("""INSERT INTO node_artifacts VALUES
                ('receipt', 'artifact-owner', 'image', :digest, 'oci-layout:receipt',
                 123, :state, 0, :at, :at)"""),
                {
                    "digest": "e" * 64,
                    "state": ModelFileState.VERIFIED.value,
                    "at": at,
                },
            )
    original = node_facts.move_artifact_facts

    def interrupted(connection: Connection) -> None:
        def stop(_conn, _cursor, statement, _parameters, _context, _many):
            if statement == "DROP TABLE node_artifacts":
                raise InterruptedError("Controller stopped before receipt retirement")

        event.listen(connection, "before_cursor_execute", stop)
        try:
            original(connection)
        finally:
            event.remove(connection, "before_cursor_execute", stop)

    url = postgres_engine.url.render_as_string(hide_password=False)
    with monkeypatch.context() as interruption:
        interruption.setattr(node_facts, "move_artifact_facts", interrupted)
        with pytest.raises(InterruptedError):
            initialize_database(url)
    with sessions() as session:
        assert inventory_items(session.get(AgentNode, "artifact-owner")).artifacts == []
    initialize_database(url)
    initialize_database(url)
    with sessions() as session:
        items = inventory_items(session.get(AgentNode, "artifact-owner"))
        assert items.hostname == "preserved.internal"
        assert items.preflight_fingerprint == "d" * 64
        assert [item.digest for item in items.artifacts] == (
            ["e" * 64] if with_receipt else []
        )
    with postgres_engine.connect() as connection:
        assert "node_artifacts" not in inspect(connection).get_table_names()


@pytest.mark.usefixtures("damaged_json_rows")
def test_authenticated_claim_rebuilds_damaged_inventory_without_blessing_old_facts(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    """Catches a moved JSON owner permanently refusing every subsequent report."""
    from sqlalchemy import update
    from vonk_agent_protocol import UnknownOutcomeError
    from vonk_control.inventory_items import inventory_items
    from vonk_control.models import AgentNode, NodeInventoryItemsSnapshot

    from .test_agent_api import NODE_A, agent_headers, make_agent_system

    client, services, _, _ = make_agent_system(tmp_path, engine=postgres_engine)
    headers = agent_headers(NODE_A, "serial-a")
    assert client.post("/agent/claim", headers=headers).status_code == 204
    with services.sessions.begin() as session:
        session.execute(
            update(NodeInventoryItemsSnapshot)
            .where(NodeInventoryItemsSnapshot.node_id == NODE_A)
            .values(agent_items={"runtime_identity": {"invalid": True}})
        )
    with services.sessions() as session, pytest.raises(UnknownOutcomeError):
        inventory_items(session.get(AgentNode, NODE_A))
    # Reconnect through the ordinary authenticated producer. Unrelated facts
    # are unknown, not fabricated or recovered through an old column reader.
    assert (
        client.post(
            "/agent/claim", headers=headers, json={"hostname": "spark-recovered"}
        ).status_code
        == 204
    )
    with services.sessions() as session:
        recovered = inventory_items(session.get(AgentNode, NODE_A))
        assert recovered.runtime_identity is not None
        assert recovered.hostname == "spark-recovered"
        assert recovered.artifacts == []
        assert recovered.preflights == []


@pytest.mark.parametrize(
    "field",
    sorted(
        {
            path[0]
            for group in InventoryRequest.EVIDENCE_GROUPS
            for path in group.paths
            if len(path) == 1
            and isinstance(
                NodeInventorySnapshot.__table__.c[path[0]].type, ContractJSON
            )
        }
    ),
)
def test_damaged_optional_inventory_keeps_capacity_and_fresh_report_recovers(
    postgres_engine: Engine, tmp_path: Path, field: str
) -> None:
    """Catches typed JSON damage refusing core capacity or poisoning fresh reports."""
    from datetime import timedelta

    from sqlalchemy import text
    from vonk_agent_protocol.inventory import InventoryRequest
    from vonk_control.inventory_repository import InventoryRepository

    from .test_agent_acceptance_postgres import _inventory
    from .test_agent_api import NODE_A, agent_headers, make_agent_system

    # Every injected field is optional evidence declared by the single wire
    # contract; no parser or admission fallback is used for the damaged bytes.
    assert any((field,) in group.paths for group in InventoryRequest.EVIDENCE_GROUPS)
    client, services, _, clock = make_agent_system(tmp_path, engine=postgres_engine)
    headers = agent_headers(NODE_A, "serial-a")
    body = _inventory(clock)
    assert (
        client.post("/agent/inventory", headers=headers, json=body).status_code == 204
    )
    repository = InventoryRepository(services.sessions)
    before = repository.latest(NODE_A, now=clock.now, maximum_age=60)
    with services.sessions.begin() as session:
        session.execute(
            text(
                f"UPDATE node_inventory_snapshots SET \"{field}\" = '42'::json WHERE id = :identity"
            ),
            {"identity": before.id},
        )
    damaged = repository.latest(NODE_A, now=clock.now, maximum_age=60)
    assert damaged.disk_free_bytes == before.disk_free_bytes
    assert damaged.host_memory_free_bytes == before.host_memory_free_bytes
    assert getattr(damaged, field) is None
    clock.now += timedelta(seconds=1)
    assert (
        client.post(
            "/agent/inventory", headers=headers, json=_inventory(clock)
        ).status_code
        == 204
    )
    recovered = repository.latest(NODE_A, now=clock.now, maximum_age=60)
    assert recovered.id != before.id
    assert recovered.observed_at == clock.now
    assert recovered.disk_free_bytes == before.disk_free_bytes
