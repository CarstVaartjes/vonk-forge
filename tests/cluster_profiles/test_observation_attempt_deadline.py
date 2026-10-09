"""Buffered parsing never adopts a complete observation after its attempt ended."""

from __future__ import annotations

import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.fleet_projection import FleetProjection
from vonk_control.models import Base
from vonk_control.strict_json import serialize_json_value

from cluster_profiles import observation_transfer_reader
from cluster_profiles.control_client import (
    ControlClient,
    ControlTransportError,
)
from cluster_profiles.control_client import client as control_client
from control.tests.observation_transfer_peer import ObservationHTTPPeer
from control.tests.test_observation_transfer import _peer


def test_late_complete_payload_validation_is_not_adopted_and_next_attempt_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'fleet.sqlite'}")
    Base.metadata.create_all(engine)
    snapshot = FleetProjection(
        sessionmaker(engine), clock=lambda: datetime(2026, 10, 7, tzinfo=UTC)
    ).read()
    engine.dispose()
    with _peer(snapshot) as app_peer:
        response = app_peer.get("/api/fleet")
    response.headers["X-Request-ID"] = "late-validation-fixture"
    response.headers["Retry-After"] = "120"
    now = [100.0]
    late = [True]
    peers: list[ObservationHTTPPeer] = []
    original = control_client.validate_control_document

    def validate(component: str, value: object) -> dict[str, object]:
        validated = original(component, value)
        if late[0]:
            now[0] = 102.0
        return validated

    def opener(
        _request: urllib.request.Request, *, timeout: float
    ) -> ObservationHTTPPeer:
        assert 0 < timeout <= 1
        peer = ObservationHTTPPeer(response)
        peers.append(peer)
        return peer

    monkeypatch.setattr(observation_transfer_reader.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(control_client, "validate_control_document", validate)
    token = tmp_path / "token"
    token.write_text("private-fixture-token", encoding="utf-8")
    token.chmod(0o600)
    client = ControlClient(
        "https://control.invalid", token, opener=opener, timeout_seconds=1
    )
    observed = None
    try:
        observed = client.request("GET", "/api/fleet")
    except ControlTransportError:
        pass
    assert observed is None
    assert now[0] == 102.0
    assert len(peers) == 1
    assert peers[0]._body.closed
    late[0] = False
    assert client.request("GET", "/api/fleet") == serialize_json_value(snapshot)
    assert len(peers) == 2 and all(peer._body.closed for peer in peers)
