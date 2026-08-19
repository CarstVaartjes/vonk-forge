from pathlib import Path


def test_migrate_database_runs_all_alembic_migrations(monkeypatch, tmp_path: Path) -> None:
    from vonk_control import compose_bootstrap

    database_url = tmp_path / "database-url"
    database_url.write_text("postgresql+psycopg://control:p%40ss@postgres/control\n")
    config_file = tmp_path / "alembic.ini"
    config_file.write_text("[alembic]\nscript_location = migrations\n")
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        compose_bootstrap.command,
        "upgrade",
        lambda config, target: calls.append(
            (config.get_main_option("sqlalchemy.url"), target)
        ),
    )

    compose_bootstrap.migrate_database(database_url, config_file)

    assert calls == [("postgresql+psycopg://control:p%40ss@postgres/control", "head")]
