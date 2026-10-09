import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError


def test_default_alembic_config_is_packaged_with_the_control_library() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from vonk_control import db

    scripts = ScriptDirectory.from_config(Config(str(db._ALEMBIC_CONFIG)))
    assert scripts.get_current_head() is not None
    assert tuple(scripts.walk_revisions())


def test_built_wheel_contains_alembic_config_next_to_installed_module(
    tmp_path: Path,
) -> None:
    project = Path(__file__).resolve().parents[1]
    subprocess.run(
        # The locked build backend in the test environment: no isolated build
        # environment, no network.
        [
            sys.executable,
            "-m",
            "hatchling",
            "build",
            "--target",
            "wheel",
            "--directory",
            str(tmp_path),
        ],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(tmp_path.glob("*.whl"))
    with zipfile.ZipFile(wheel) as package:
        package.extractall(tmp_path / "installed")
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    scripts = ScriptDirectory.from_config(
        Config(str(tmp_path / "installed" / "vonk_control" / "alembic.ini"))
    )
    assert scripts.get_current_head() is not None
    assert tuple(scripts.walk_revisions())


def test_upgrade_schema_runs_the_linear_alembic_head(
    monkeypatch, tmp_path: Path
) -> None:
    from vonk_control import db

    config_file = tmp_path / "alembic.ini"
    config_file.write_text("[alembic]\nscript_location = migrations\n")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        db.command,
        "upgrade",
        lambda config, target: calls.append(
            (config.get_main_option("sqlalchemy.url"), target)
        ),
    )

    db.upgrade_schema(
        "postgresql+psycopg://control:p%40ss@postgres/control",
        config_path=config_file,
    )

    assert calls == [("postgresql+psycopg://control:p%40ss@postgres/control", "head")]


def test_database_startup_retry_is_bounded_and_logs_only_connection_failures(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from vonk_control import db

    now = 0.0
    calls = 0
    waits: list[float] = []

    def monotonic() -> float:
        return now

    def sleep(seconds: float) -> None:
        nonlocal now
        waits.append(seconds)
        now += seconds

    def operation() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise OperationalError("connect", {}, OSError("temporary DNS failure"))
        return "ready"

    assert (
        db.run_with_database_startup_retry(
            operation,
            timeout_seconds=10,
            sleep=sleep,
            monotonic=monotonic,
            label="PostgreSQL",
        )
        == "ready"
    )
    assert calls == 3
    assert waits == [0.5, 1.0]
    assert 0 < now < 10
    assert (
        db.run_with_database_startup_retry(lambda: "fresh", timeout_seconds=10)
        == "fresh"
    )


def test_database_startup_retry_re_raises_after_deadline() -> None:
    from vonk_control import db

    now = 0.0
    waits: list[float] = []
    failure = OperationalError("connect", {}, OSError("temporary DNS failure"))

    def sleep(seconds: float) -> None:
        nonlocal now
        waits.append(seconds)
        now += seconds

    with pytest.raises(OperationalError):
        db.run_with_database_startup_retry(
            lambda: (_ for _ in ()).throw(failure),
            timeout_seconds=0.75,
            sleep=sleep,
            monotonic=lambda: now,
        )

    assert now == 0.75
    assert sum(waits) == 0.75
    assert (
        db.run_with_database_startup_retry(lambda: "fresh", timeout_seconds=1)
        == "fresh"
    )


def test_database_startup_retry_rejects_an_unbounded_timeout() -> None:
    from vonk_control import db

    with pytest.raises(Exception) as _ending:
        db.run_with_database_startup_retry(lambda: None, timeout_seconds=901)


def test_database_startup_retry_does_not_mask_permission_failures() -> None:
    from vonk_control import db

    calls = 0

    def operation() -> None:
        nonlocal calls
        calls += 1
        raise PermissionError("database secret is not readable")

    with pytest.raises(Exception) as _ending:
        db.run_with_database_startup_retry(operation)

    assert calls == 1


def test_build_engine_bounds_every_wait_only_on_postgres(monkeypatch) -> None:
    """Every PostgreSQL wait must be finite, and the pool explicitly bounded.

    PostgreSQL accepts the per-connection timeout options; SQLite rejects both
    the server options and an explicit pool size, so it keeps the default pool.
    """

    from vonk_control import db

    calls: list[tuple[str, dict[str, object]]] = []
    engines: list[Engine] = []

    def record(url: str, **kwargs: object) -> Engine:
        calls.append((url, kwargs))
        engine = create_engine(url, **kwargs)
        engines.append(engine)
        return engine

    monkeypatch.setattr(db, "create_engine", record)

    try:
        db.build_engine("postgresql+psycopg://control@postgres/control")
        db.build_engine("sqlite+pysqlite:///:memory:")
    finally:
        for engine in engines:
            engine.dispose()

    assert calls == [
        (
            "postgresql+psycopg://control@postgres/control",
            {
                "pool_pre_ping": True,
                "pool_size": 5,
                "max_overflow": 10,
                "pool_timeout": 30.0,
                "connect_args": {
                    "connect_timeout": 30,
                    "options": (
                        "-c application_name=vonk:control"
                        " -c lock_timeout=30000"
                        " -c statement_timeout=120000"
                        " -c transaction_timeout=300000"
                        " -c idle_in_transaction_session_timeout=60000"
                    ),
                },
            },
        ),
        (
            "sqlite+pysqlite:///:memory:",
            {"pool_pre_ping": True, "connect_args": {}},
        ),
    ]


def test_build_engine_sets_every_finite_budget_on_the_server(postgres_engine) -> None:
    """The bounds must reach PostgreSQL, not just the engine's argument list."""

    from vonk_control import db

    engine = db.build_engine(postgres_engine.url.render_as_string(hide_password=False))
    try:
        with engine.connect() as connection:
            rows = connection.exec_driver_sql(
                "SELECT name, setting FROM pg_settings WHERE name IN ("
                "'lock_timeout', 'statement_timeout', 'transaction_timeout',"
                " 'idle_in_transaction_session_timeout')"
            ).all()
            settings = {str(row[0]): str(row[1]) for row in rows}
    finally:
        engine.dispose()

    assert settings == {
        "lock_timeout": "30000",
        "statement_timeout": "120000",
        "transaction_timeout": "300000",
        "idle_in_transaction_session_timeout": "60000",
    }
