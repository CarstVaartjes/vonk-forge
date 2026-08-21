from pathlib import Path


def test_default_alembic_config_is_packaged_with_the_control_library() -> None:
    from vonk_control import db

    assert db._ALEMBIC_CONFIG == Path(db.__file__).resolve().parent / "alembic.ini"


def test_upgrade_schema_runs_the_single_alembic_head(
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
