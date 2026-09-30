from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from .conftest import (
    POSTGRES_IMAGE,
    _docker_unavailable,
    postgres_database_name,
)


def test_postgres_runtime_is_the_deployed_pin() -> None:
    lock = json.loads(
        (
            Path(__file__).resolve().parents[2] / "deploy/compose/images.lock.json"
        ).read_text(encoding="utf-8")
    )
    assert POSTGRES_IMAGE == lock["images"]["postgres"]
    assert re.fullmatch(r"postgres:\d+\.\d+@sha256:[0-9a-f]{64}", POSTGRES_IMAGE)


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
    assert version.startswith(POSTGRES_IMAGE.removeprefix("postgres:") + " ") or (
        version == POSTGRES_IMAGE.removeprefix("postgres:")
    )
