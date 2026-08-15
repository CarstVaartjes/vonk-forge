from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def upgrade_to(revision: str, database: str) -> None:
    command.upgrade(_config(database), revision)


def downgrade_to(revision: str, database: str) -> None:
    command.downgrade(_config(database), revision)


def tables(database: str) -> set[str]:
    return set(inspect(create_engine(database)).get_table_names())


def test_agent_migration_is_reversible(tmp_path: Path) -> None:
    database = f"sqlite:///{tmp_path / 'control.sqlite'}"
    agent_tables = {
        "agent_nodes",
        "agent_certificates",
        "agent_operations",
        "agent_operation_attempts",
    }

    upgrade_to("0001_operational_state", database)
    original_tables = tables(database)
    assert not (agent_tables & original_tables)

    upgrade_to("0002_agent_operations", database)
    assert agent_tables <= tables(database)

    downgrade_to("0001_operational_state", database)

    assert tables(database) == original_tables

    upgrade_to("0002_agent_operations", database)
    assert agent_tables <= tables(database)


def test_current_model_metadata_matches_head_schema(tmp_path: Path) -> None:
    from vonk_control.models import Base

    database = f"sqlite:///{tmp_path / 'control.sqlite'}"
    upgrade_to("head", database)

    assert set(Base.metadata.tables) == tables(database) - {"alembic_version"}
    engine = create_engine(database)
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []


def test_agent_runtime_identity_migration_is_reversible(tmp_path: Path) -> None:
    database = f"sqlite:///{tmp_path / 'runtime-identity.sqlite'}"
    engine = create_engine(database)
    upgrade_to("0009_reconciliation_execution", database)
    before = {
        column["name"]
        for column in inspect(engine).get_columns("agent_nodes")
    }

    upgrade_to("0010_agent_runtime_identity", database)
    after = {
        column["name"]
        for column in inspect(engine).get_columns("agent_nodes")
    }
    assert after == before | {
        "active_slot",
        "agent_sha256",
        "build_digest",
        "platform_version",
        "supervisor_generation",
    }

    downgrade_to("0009_reconciliation_execution", database)
    assert {
        column["name"]
        for column in inspect(engine).get_columns("agent_nodes")
    } == before


def test_agent_models_capture_fenced_operation_state() -> None:
    from vonk_control.models import (
        AgentCertificate,
        AgentNode,
        AgentOperation,
        AgentOperationAttempt,
    )

    assert AgentNode.__table__.primary_key.columns.keys() == ["node_id"]
    assert AgentNode.__table__.c.capabilities.default is not None
    assert "private_key" not in AgentCertificate.__table__.c
    assert AgentOperation.__table__.c.parent_job_id.foreign_keys
    assert AgentOperation.__table__.c.node_id.foreign_keys
    assert AgentOperation.__table__.c.retry_disposition.nullable
    assert AgentOperation.__table__.c.retry_disposition_attempt.nullable
    assert AgentOperationAttempt.__table__.c.fence.unique
    assert any(
        constraint.columns.keys() == ["operation_id", "attempt"]
        for constraint in AgentOperationAttempt.__table__.constraints
    )


def test_retry_disposition_migration_is_reversible(tmp_path: Path) -> None:
    database = f"sqlite:///{tmp_path / 'control.sqlite'}"
    upgrade_to("0002_agent_operations", database)
    before = {column["name"] for column in inspect(create_engine(database)).get_columns("agent_operations")}

    upgrade_to("0003_retry_disposition", database)
    after = {column["name"] for column in inspect(create_engine(database)).get_columns("agent_operations")}
    assert after == before | {"retry_disposition", "retry_disposition_attempt"}

    downgrade_to("0002_agent_operations", database)
    downgraded = {column["name"] for column in inspect(create_engine(database)).get_columns("agent_operations")}
    assert downgraded == before


def test_enrollment_migration_is_reversible_and_preserves_model_parity(tmp_path: Path) -> None:
    database = f"sqlite:///{tmp_path / 'control.sqlite'}"
    enrollment_tables = {"agent_enrollment_grants", "agent_enrollments"}

    upgrade_to("0003_retry_disposition", database)
    before = tables(database)
    assert not (enrollment_tables & before)

    upgrade_to("0004_agent_enrollment", database)
    assert enrollment_tables <= tables(database)
    grants = {column["name"] for column in inspect(create_engine(database)).get_columns("agent_enrollment_grants")}
    assert grants == {"id", "node_id", "token_digest", "created_by", "created_at", "expires_at", "consumed_at"}
    assert "token" not in grants

    downgrade_to("0003_retry_disposition", database)
    assert tables(database) == before

    upgrade_to("head", database)
    from vonk_control.models import Base

    engine = create_engine(database)
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []


def test_certificate_rotation_migration_backfills_active_generation_and_is_reversible(
    tmp_path: Path,
) -> None:
    database = f"sqlite:///{tmp_path / 'control.sqlite'}"
    engine = create_engine(database)
    upgrade_to("0004_agent_enrollment", database)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO agent_nodes (node_id,state,capabilities) "
            "VALUES ('spk_0123456789abcdef0123456789abcdef','active','[]')"
        ))
        connection.execute(text(
            "INSERT INTO agent_certificates "
            "(serial,node_id,not_before,not_after,fingerprint,revoked_at) VALUES "
            "('serial-1','spk_0123456789abcdef0123456789abcdef',"
            "'2026-08-03 00:00:00','2026-08-04 00:00:00','fingerprint-1',"
            "'2026-08-04 00:00:00'),"
            "('serial-2','spk_0123456789abcdef0123456789abcdef',"
            "'2026-08-04 00:00:00','2026-08-05 00:00:00','fingerprint-2',NULL)"
        ))

    upgrade_to("0005_certificate_rotation", database)
    assert "agent_certificate_rotations" in tables(database)
    columns = {column["name"] for column in inspect(engine).get_columns("agent_certificates")}
    assert {
        "state", "generation", "certificate_pem", "chain_pem",
        "csr_public_key_fingerprint",
    } <= columns
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT serial,state,generation FROM agent_certificates "
            "ORDER BY generation"
        )).all() == [
            ("serial-1", "active", 1),
            ("serial-2", "active", 2),
        ]
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO agent_certificates "
            "(serial,node_id,not_before,not_after,fingerprint,revoked_at,"
            "state,generation,certificate_pem,chain_pem,csr_public_key_fingerprint) "
            "VALUES ('serial-3','spk_0123456789abcdef0123456789abcdef',"
            "'2026-08-04 00:00:00','2026-08-06 00:00:00','fingerprint-3',NULL,"
            "'staged',3,'certificate','chain','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')"
        ))

    downgrade_to("0004_agent_enrollment", database)
    assert "agent_certificate_rotations" not in tables(database)
    downgraded = {column["name"] for column in inspect(engine).get_columns("agent_certificates")}
    assert not {
        "state", "generation", "certificate_pem", "chain_pem",
        "csr_public_key_fingerprint",
    } & downgraded
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT serial,revoked_at IS NULL FROM agent_certificates ORDER BY serial"
        )).all() == [
            ("serial-1", False),
            ("serial-2", True),
            ("serial-3", False),
        ]
        assert connection.execute(text(
            "SELECT certificate.serial FROM agent_certificates AS certificate "
            "JOIN agent_nodes AS node ON node.node_id = certificate.node_id "
            "WHERE certificate.serial = 'serial-3' "
            "AND certificate.node_id = 'spk_0123456789abcdef0123456789abcdef' "
            "AND certificate.fingerprint = 'fingerprint-3' "
            "AND certificate.revoked_at IS NULL "
            "AND certificate.not_before <= '2026-08-05 00:00:00' "
            "AND certificate.not_after > '2026-08-05 00:00:00' "
            "AND node.state = 'active' AND node.revoked_at IS NULL"
        )).scalar_one_or_none() is None

    upgrade_to("0005_certificate_rotation", database)
    assert "agent_certificate_rotations" in tables(database)


def test_issued_certificate_revocation_evidence_migration_is_bounded_and_reversible(
    tmp_path: Path,
) -> None:
    database = f"sqlite:///{tmp_path / 'control.sqlite'}"
    engine = create_engine(database)
    upgrade_to("0006_reconciliation_graph", database)
    assert "agent_issued_certificate_revocations" not in tables(database)

    upgrade_to("0007_issued_revocations", database)
    assert "agent_issued_certificate_revocations" in tables(database)
    columns = {
        column["name"]
        for column in inspect(engine).get_columns(
            "agent_issued_certificate_revocations"
        )
    }
    assert columns == {
        "serial",
        "node_id",
        "provider_request_id",
        "fingerprint",
        "generation",
        "state",
        "created_at",
        "updated_at",
        "ca_revoked_at",
    }
    assert not {"certificate_pem", "chain_pem", "private_key"} & columns

    downgrade_to("0006_reconciliation_graph", database)
    assert "agent_issued_certificate_revocations" not in tables(database)


def test_resolved_plan_migration_follows_issued_revocations_and_is_reversible(
    tmp_path: Path,
) -> None:
    database = f"sqlite:///{tmp_path / 'control.sqlite'}"
    engine = create_engine(database)
    upgrade_to("0007_issued_revocations", database)
    assert "plan_digest" not in {
        column["name"] for column in inspect(engine).get_columns("reconciliations")
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO reconciliations "
                "(id,base_commit,status,summary,created_at) VALUES "
                "('legacy-plan','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',"
                "'planned','{}','2026-08-05 00:00:00')"
            )
        )

    upgrade_to("0008_resolved_plan", database)
    columns = {
        column["name"] for column in inspect(engine).get_columns("reconciliations")
    }
    assert {"plan_digest", "resolved_plan", "completion_generation"} <= columns
    assert "reconciliation_completion_generation" in tables(database)
    indexes = {
        index["name"]: index
        for index in inspect(engine).get_indexes("reconciliations")
    }
    assert indexes["ix_reconciliations_plan_digest"]["unique"] == 1
    assert indexes["ix_reconciliations_completion_generation"]["unique"] == 1
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT plan_digest,resolved_plan FROM reconciliations "
                "WHERE id='legacy-plan'"
            )
        ).one() == (None, None)
        assert connection.execute(
            text(
                "SELECT singleton_id,last_generation "
                "FROM reconciliation_completion_generation"
            )
        ).one() == (1, 0)
        assert connection.execute(
            text(
                "SELECT completion_generation FROM reconciliations "
                "WHERE id='legacy-plan'"
            )
        ).scalar_one() is None

    downgrade_to("0007_issued_revocations", database)
    assert "reconciliation_completion_generation" not in tables(database)
    assert not {"plan_digest", "resolved_plan", "completion_generation"} & {
        column["name"] for column in inspect(engine).get_columns("reconciliations")
    }
    upgrade_to("head", database)
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT status FROM reconciliations WHERE id='legacy-plan'")
        ).scalar_one() == "planned"
