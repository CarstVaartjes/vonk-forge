from __future__ import annotations

import re

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.conftest import (
    POSTGRES_IMAGE,
    _docker_unavailable,
    postgres_database_name,
)


def test_postgres_runtime_is_immutable_18_3() -> None:
    assert POSTGRES_IMAGE == (
        "postgres:18.3@sha256:"
        "7e32e9833a6fb1c92c32552794cb6ed569d51b445a54907d35fc112ef39684db"
    )


def test_postgres_database_names_are_unique_safe_identifiers() -> None:
    first = postgres_database_name()
    second = postgres_database_name()

    assert first != second
    assert re.fullmatch(r"vonk_test_[0-9a-f]{32}", first)
    assert re.fullmatch(r"vonk_test_[0-9a-f]{32}", second)


def test_unavailable_docker_is_an_explicit_local_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CI", raising=False)

    with pytest.raises(pytest.skip.Exception, match="Docker unavailable"):
        _docker_unavailable("Docker unavailable")


def test_unavailable_docker_fails_in_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI", "true")

    with pytest.raises(pytest.fail.Exception, match="Docker unavailable"):
        _docker_unavailable("Docker unavailable")


def test_postgres_engine_uses_isolated_postgres_18_database(
    postgres_engine: Engine,
) -> None:
    with postgres_engine.connect() as connection:
        database, version = connection.execute(
            text("SELECT current_database(), current_setting('server_version')")
        ).one()

    assert re.fullmatch(r"vonk_test_[0-9a-f]{32}", database)
    assert version.startswith("18.3")
