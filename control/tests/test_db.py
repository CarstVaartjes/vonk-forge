from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError


def test_default_alembic_config_is_packaged_with_the_control_library() -> None:
    from vonk_control import db

    assert db._ALEMBIC_CONFIG == Path(db.__file__).resolve().parent / "alembic.ini"


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
    assert "PostgreSQL unavailable during startup" in capsys.readouterr().err


def test_database_startup_retry_re_raises_after_deadline() -> None:
    from vonk_control import db

    now = 0.0
    waits: list[float] = []
    failure = OperationalError("connect", {}, OSError("temporary DNS failure"))

    def sleep(seconds: float) -> None:
        nonlocal now
        waits.append(seconds)
        now += seconds

    with pytest.raises(OperationalError) as raised:
        db.run_with_database_startup_retry(
            lambda: (_ for _ in ()).throw(failure),
            timeout_seconds=0.75,
            sleep=sleep,
            monotonic=lambda: now,
        )

    assert raised.value is failure
    assert waits == [0.5, 0.25]


def test_database_startup_retry_rejects_an_unbounded_timeout() -> None:
    from vonk_control import db

    with pytest.raises(ValueError, match="safe bound"):
        db.run_with_database_startup_retry(lambda: None, timeout_seconds=901)


def test_database_startup_retry_does_not_mask_permission_failures() -> None:
    from vonk_control import db

    calls = 0

    def operation() -> None:
        nonlocal calls
        calls += 1
        raise PermissionError("database secret is not readable")

    with pytest.raises(PermissionError, match="not readable"):
        db.run_with_database_startup_retry(operation)

    assert calls == 1


def test_build_engine_bounds_every_wait_only_on_postgres(monkeypatch) -> None:
    """Every PostgreSQL wait must be finite, and the pool explicitly bounded.

    PostgreSQL accepts the per-connection timeout options; SQLite rejects both
    the server options and an explicit pool size, so it keeps the default pool.
    """

    from vonk_control import db

    calls: list[tuple[str, dict[str, object]]] = []

    def record(url: str, **kwargs: object) -> object:
        calls.append((url, kwargs))
        return object()

    monkeypatch.setattr(db, "create_engine", record)

    db.build_engine("postgresql+psycopg://control@postgres/control")
    db.build_engine("sqlite+pysqlite:///:memory:")

    assert calls == [
        (
            "postgresql+psycopg://control@postgres/control",
            {
                "pool_pre_ping": True,
                "pool_size": 5,
                "max_overflow": 10,
                "pool_timeout": 30.0,
                "connect_args": {
                    "options": (
                        "-c lock_timeout=30000"
                        " -c statement_timeout=120000"
                        " -c transaction_timeout=300000"
                        " -c idle_in_transaction_session_timeout=60000"
                    )
                },
            },
        ),
        (
            "sqlite+pysqlite:///:memory:",
            {"pool_pre_ping": True, "connect_args": {}},
        ),
    ]


def test_database_wait_budgets_refuse_out_of_range_values(monkeypatch) -> None:
    """Configuration is the owner, and an invalid budget is refused, not clamped."""

    from vonk_control.settings import SettingsError, database_wait_budgets

    monkeypatch.delenv("VONK_DATABASE_LOCK_TIMEOUT_MS", raising=False)
    monkeypatch.setenv("VONK_DATABASE_LOCK_TIMEOUT_MS", "0")
    with pytest.raises(SettingsError, match="VONK_DATABASE_LOCK_TIMEOUT_MS"):
        database_wait_budgets()

    monkeypatch.setenv("VONK_DATABASE_LOCK_TIMEOUT_MS", "30000")
    monkeypatch.setenv("VONK_DATABASE_ADMISSION_LOCK_TIMEOUT_MS", "30001")
    with pytest.raises(SettingsError, match="admission lock budget"):
        database_wait_budgets()
    monkeypatch.setenv("VONK_DATABASE_ADMISSION_LOCK_TIMEOUT_MS", "750")
    monkeypatch.setenv("VONK_DATABASE_POOL_SIZE", "1000")
    with pytest.raises(SettingsError, match="VONK_DATABASE_POOL_SIZE"):
        database_wait_budgets()

    # A lock budget above the statement budget would make the statement bound
    # unreachable, so the combination is refused rather than reordered.
    monkeypatch.setenv("VONK_DATABASE_POOL_SIZE", "5")
    monkeypatch.setenv("VONK_DATABASE_STATEMENT_TIMEOUT_MS", "5000")
    with pytest.raises(SettingsError, match="lock <= statement <= transaction"):
        database_wait_budgets()


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
