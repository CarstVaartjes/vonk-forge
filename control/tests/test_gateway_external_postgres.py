"""External model publication on PostgreSQL: crash points that repeat no effect and lose nothing."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from vonk_agent_protocol.state_machines import ExternalPublicationState
from vonk_control.models.gateway import GatewayExternalDeployment
from vonk_control.recipe_routes.service import RecipeRouteService

from .recipe_routes_support import NOW, MutableClock, _live_models
from .test_gateway_external_publication import (
    _digest,
    _generations,
    _row,
    _seed,
    _service,
    _spec,
)


class Crash(BaseException):
    """The process died: nothing downstream of this point runs."""


def _crash_on_ack(*_arguments: object) -> None:
    raise Crash


def _pending(engine, tmp_path: Path, monkeypatch, clock) -> RecipeRouteService:
    service = _service(tmp_path, monkeypatch, clock, engine=engine)
    _seed(service.sessions, _spec())
    return service


def test_a_restart_after_the_accept_publishes_it_once(
    postgres_engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = MutableClock(NOW)
    first = _pending(postgres_engine, tmp_path, monkeypatch, clock)
    # The process died after the accept: no tick ever ran. A new one finds the intent.
    assert _row(first, "partner-large") is not None
    restarted = _service(tmp_path, monkeypatch, clock, engine=postgres_engine)
    assert restarted.maintain() is True
    assert _generations(tmp_path / "live") == 1
    again = _service(tmp_path, monkeypatch, clock, engine=postgres_engine)
    assert again.maintain() is False
    assert _generations(tmp_path / "live") == 1
    row = _row(again, "partner-large")
    assert row is not None
    assert row.state == ExternalPublicationState.PUBLISHED.value


def test_a_crash_after_the_publish_and_before_the_ack_adopts_the_same_generation(
    postgres_engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = MutableClock(NOW)
    dying = _service(
        tmp_path, monkeypatch, clock, engine=postgres_engine, ack=_crash_on_ack
    )
    _seed(dying.sessions, _spec())
    with pytest.raises(Crash):
        dying.maintain()
    root = tmp_path / "live"
    written = _digest(root)
    assert _generations(root) == 1
    row = _row(dying, "partner-large")
    assert row is not None
    assert row.state == ExternalPublicationState.PENDING.value  # nothing was blamed

    restarted = _service(tmp_path, monkeypatch, clock, engine=postgres_engine)
    clock.now = NOW.replace(minute=NOW.minute + 5)
    assert restarted.maintain() is True
    assert _digest(root) == written  # the exact written bytes were acknowledged
    assert _generations(root) == 1
    row = _row(restarted, "partner-large")
    assert row is not None
    assert row.state == ExternalPublicationState.PUBLISHED.value


def test_a_crash_after_the_ack_and_before_the_record_loses_nothing_and_a_removed_entry_stays_gone(
    postgres_engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = MutableClock(NOW)
    service = _pending(postgres_engine, tmp_path, monkeypatch, clock)
    root = tmp_path / "live"
    record = RecipeRouteService._record_completion

    def die(self, publication, generation):
        raise Crash

    monkeypatch.setattr(RecipeRouteService, "_record_completion", die)
    with pytest.raises(Crash):
        service.maintain()
    monkeypatch.setattr(RecipeRouteService, "_record_completion", record)
    written = _digest(root)
    clock.now = NOW.replace(minute=NOW.minute + 5)
    assert (
        _service(tmp_path, monkeypatch, clock, engine=postgres_engine).maintain()
        is True
    )
    assert _digest(root) == written
    assert _generations(root) == 1
    assert _live_models(root) == ["partner-large"]

    # Removal: die after its generation was acknowledged, before the row was deleted.
    with sessionmaker(postgres_engine).begin() as session:
        stored = session.scalar(select(GatewayExternalDeployment))
        assert stored is not None
        stored.enabled = False
        stored.removal_requested = True
    monkeypatch.setattr(RecipeRouteService, "_record_completion", die)
    with pytest.raises(Crash):
        _service(tmp_path, monkeypatch, clock, engine=postgres_engine).maintain()
    monkeypatch.setattr(RecipeRouteService, "_record_completion", record)
    assert _live_models(root) == []
    clock.now = NOW.replace(minute=NOW.minute + 10)
    assert (
        _service(tmp_path, monkeypatch, clock, engine=postgres_engine).maintain()
        is True
    )
    assert _row(service, "partner-large") is None
    assert _live_models(root) == []
    assert (
        _service(tmp_path, monkeypatch, clock, engine=postgres_engine).maintain()
        is False
    )
