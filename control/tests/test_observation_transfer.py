"""Connected byte/receipt proofs; executed in hosted Controller CI."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Request, Response
from jsonschema import Draft202012Validator
from pydantic import JsonValue, TypeAdapter
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import canonical_message
from vonk_control.api import create_app
from vonk_control.auth import Actor, TokenCodec
from vonk_control.fleet_projection import (
    FleetProjection,
    FleetSnapshot,
    UnavailableRecipePresence,
)
from vonk_control.fleet_stream_contract import FleetRefreshEvent
from vonk_control.models import AgentNode, Base
from vonk_control.observation_transfer import (
    ObservationTransferChunk,
    ObservationTransferComplete,
    ObservationTransferRecord,
    ObservationTransferStart,
    observation_response,
)
from vonk_control.strict_json import serialize_json_value

from cluster_profiles.control_client import (
    ControlClient,
    ControlClientError,
    source_schema_validator,
)
from cluster_profiles.control_limits import MAX_CONTROL_DOCUMENT_BYTES

from .observation_transfer_peer import (
    ObservationHTTPPeer,
    ObservationHTTPResponse,
    fleet_authority,
)
from .test_platform_observation import Jobs

NOW = datetime(2026, 10, 7, tzinfo=UTC)
NODE = "spk_" + "1" * 32


def _large_snapshot(tmp_path: Path) -> FleetSnapshot:
    engine = create_engine(f"sqlite:///{tmp_path / 'fleet.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(AgentNode(node_id=NODE, state="active", last_seen_at=NOW))
    snapshot = FleetProjection(sessions, clock=lambda: NOW).read()
    engine.dispose()
    # Legal single-row canonical membership exceeds one reader record. It must
    # split transport bytes, never erase unavailable group evidence or members.
    members = [f"spk_{index:032x}" for index in range(500)]
    snapshot.nodes[0].installed = [
        UnavailableRecipePresence(
            installation_id=f"installation-{index}",
            projection_issue="Stored group plan is temporarily unreadable",
            title="🍃" * 200,
            member_node_ids=members,
            expected_rank_count=500,
            present_ranks=list(range(500)),
            complete=None,
        )
        for index in range(64)
    ]
    assert (
        len(json.dumps(serialize_json_value(snapshot)).encode())
        > MAX_CONTROL_DOCUMENT_BYTES
    )
    return snapshot


class Projection:
    def __init__(self, snapshot: FleetSnapshot) -> None:
        self.snapshot = snapshot

    def read(self) -> FleetSnapshot:
        return self.snapshot


def _app_peer(snapshot: FleetSnapshot) -> tuple[FastAPI, TestClient]:
    codec = TokenCodec(b"k" * 32)
    token = codec.issue(Actor("viewer", "viewer"), ttl_seconds=100, now=0)
    app = create_app(
        jobs=Jobs(),
        tokens=codec,
        now=lambda: 10,
        fleet_projection=Projection(snapshot),
    )
    return app, TestClient(app, headers={"Authorization": f"Bearer {token}"})


def _peer(snapshot: FleetSnapshot) -> TestClient:
    return _app_peer(snapshot)[1]


def _client(tmp_path: Path, response: ObservationHTTPResponse) -> ControlClient:
    token = tmp_path / "token"
    token.write_text("fixture-token")
    token.chmod(0o600)
    return ControlClient(
        "https://control.invalid",
        token,
        opener=lambda _request, **_kwargs: ObservationHTTPPeer(response),
    )


def test_large_indivisible_fleet_roundtrips_full_canonical_membership(tmp_path):
    snapshot = _large_snapshot(tmp_path)
    response = _peer(snapshot).get("/api/fleet")
    assert response.status_code == 200
    lines = response.content.splitlines(keepends=True)
    assert len(lines) >= 4
    assert all(len(line) <= MAX_CONTROL_DOCUMENT_BYTES for line in lines)
    for line in lines:
        ObservationTransferRecord.model_validate_json(line, strict=True)
    client = _client(tmp_path, response)
    observed = client.request("GET", "/api/fleet")
    assert fleet_authority(observed) == snapshot
    assert fleet_authority(client.fleet().to_dict()) == snapshot


@pytest.mark.parametrize(
    "fault",
    [
        "missing-final",
        "digest",
        "reordered",
        "duplicate",
        "cross-identity",
        "after-final",
    ],
)
def test_partial_or_corrupt_transfer_cannot_be_a_complete_snapshot_and_retry_recovers(
    tmp_path, fault
):
    snapshot = _large_snapshot(tmp_path)
    response = _peer(snapshot).get("/api/fleet")
    records = [json.loads(line) for line in response.content.splitlines()]
    if fault == "missing-final":
        records.pop()
    elif fault == "digest":
        records[-1]["sha256"] = "0" * 64
    elif fault == "reordered":
        records[1], records[2] = records[2], records[1]
    elif fault == "duplicate":
        records.insert(2, records[1])
    elif fault == "cross-identity":
        records[1]["transfer_id"] = "11111111-1111-4111-8111-111111111111"
    else:
        records.append(records[1])
    damaged = response.__class__(
        200,
        headers=response.headers,
        content=b"".join(
            json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode()
            + b"\n"
            for record in records
        ),
        request=response.request,
    )
    client = _client(tmp_path, damaged)
    peers = []

    def opener(_request, **_kwargs):
        peer = ObservationHTTPPeer(damaged if not peers else response)
        peers.append(peer)
        return peer

    client._opener = opener
    assert fleet_authority(client.request("GET", "/api/fleet")) == snapshot
    assert 1 < len(peers) <= 3
    assert all(peer._body.closed for peer in peers)
    assert fleet_authority(client.request("GET", "/api/fleet")) == snapshot
    assert fleet_authority(client.request("GET", "/api/fleet")) == snapshot


def test_capture_is_frozen_before_first_network_suspension(tmp_path):
    snapshot = _large_snapshot(tmp_path)
    original = serialize_json_value(snapshot)
    response = observation_response(snapshot, resource="fleet")
    snapshot.nodes.clear()
    snapshot.event_cursor += 1

    async def collect() -> bytes:
        pieces: list[bytes] = []
        async for part in response.body_iterator:
            pieces.append(part.encode() if isinstance(part, str) else bytes(part))
        return b"".join(pieces)

    peer = Response(
        200,
        headers={"Content-Type": response.media_type},
        content=asyncio.run(collect()),
        request=Request("GET", "https://control.invalid/api/fleet"),
    )
    assert _client(tmp_path, peer).request("GET", "/api/fleet") == original


@pytest.mark.parametrize(
    "data", ["AB==", "AAB=", "AAAA\n", "AAAA ", "", "A===", "===="]
)
def test_base64_lexical_and_padding_constraints_are_canonical_schema_rules(data):
    record = {
        "type": "chunk",
        "transfer_id": "11111111-1111-4111-8111-111111111111",
        "ordinal": 0,
        "data": data,
    }
    with pytest.raises(Exception) as _ending:
        ObservationTransferChunk.model_validate(record, strict=True)
    assert list(
        Draft202012Validator(ObservationTransferChunk.model_json_schema()).iter_errors(
            record
        )
    )


def test_transfer_identity_and_hash_owner_and_export_reject_terminal_newline():
    start = {
        "type": "start",
        "transfer_id": "11111111-1111-4111-8111-111111111111\n",
        "resource": "fleet",
        "encoding": "base64-canonical-json-utf8-v1",
    }
    complete = {
        "type": "complete",
        "transfer_id": "11111111-1111-4111-8111-111111111111",
        "chunks": 1,
        "bytes": 1,
        "sha256": "0" * 64 + "\n",
    }
    for model, record in [
        (ObservationTransferStart, start),
        (ObservationTransferComplete, complete),
    ]:
        with pytest.raises(Exception) as _ending:
            model.model_validate(record, strict=True)
        assert list(Draft202012Validator(model.model_json_schema()).iter_errors(record))


def test_whole_membership_has_no_observation_only_512_group_cap(tmp_path):
    from vonk_control.fleet_projection import UnavailableRunPresence

    snapshot = _large_snapshot(tmp_path)
    node = snapshot.nodes[0]
    node.installed = [
        UnavailableRecipePresence(
            installation_id=f"installation-{index}",
            projection_issue="Stored installation evidence is temporarily unreadable",
            member_node_ids=[node.id],
            expected_rank_count=1,
            present_ranks=[0],
            complete=None,
        )
        for index in range(513)
    ]
    node.loaded = [
        UnavailableRunPresence(
            run_id=f"run-{index}",
            projection_issue="Stored run evidence is temporarily unreadable",
            member_node_ids=[node.id],
            expected_rank_count=1,
            present_ranks=[0],
            healthy=None,
        )
        for index in range(513)
    ]
    response = _peer(snapshot).get("/api/fleet")
    assert response.status_code == 200
    assert (
        fleet_authority(_client(tmp_path, response).request("GET", "/api/fleet"))
        == snapshot
    )


@pytest.mark.parametrize(
    ("path", "media", "component", "resource"),
    [
        (
            "/api/fleet",
            "application/x-vonk-observation+ndjson",
            "ObservationTransferRecord",
            "fleet",
        ),
        (
            "/api/platform",
            "application/x-vonk-observation+ndjson",
            "ObservationTransferRecord",
            "platform",
        ),
        ("/api/fleet/stream", "text/event-stream", "FleetStreamEvent", None),
    ],
)
def test_stream_response_schema_validates_its_actual_canonical_record(
    tmp_path, path, media, component, resource
):
    # FastAPI's raw non-JSON response default must not intersect the owning
    # decoded record model with type:string. This is the actual app graph,
    # including the response declaration, not a second consumer schema.
    app, peer = _app_peer(_large_snapshot(tmp_path))
    with peer:
        graph = app.openapi()
    declared = graph["paths"][path]["get"]["responses"]["200"]["content"][media][
        "schema"
    ]
    assert declared == {"$ref": f"#/components/schemas/{component}"}
    model = (
        FleetRefreshEvent(reset_reason="initial", event_cursor=0)
        if resource is None
        else ObservationTransferStart(
            type="start",
            transfer_id=str(uuid4()),
            resource=resource,
            encoding="base64-canonical-json-utf8-v1",
        )
    )
    validator = source_schema_validator({"components": graph["components"], **declared})
    record = TypeAdapter(JsonValue).validate_json(canonical_message(model), strict=True)
    assert validator.is_valid(record)
    assert not validator.is_valid(json.dumps(record))


@pytest.mark.parametrize("path", ["/api/fleet", "/api/platform", "/api/fleet/stream"])
def test_stream_authentication_error_retains_declared_json_media(tmp_path, path):
    snapshot = _large_snapshot(tmp_path)
    app, peer = _app_peer(snapshot)
    with peer:
        graph = app.openapi()
        response = peer.get(path, headers={"Authorization": "Bearer invalid"})
    assert response.status_code == 401
    assert response.headers["content-type"].partition(";")[0] == "application/json"
    content = graph["paths"][path]["get"]["responses"]["401"]["content"]
    assert set(content) == {"application/json"}
    validation_content = graph["paths"][path]["get"]["responses"]["422"]["content"]
    assert set(validation_content) == {"application/json"}
    validator = source_schema_validator(
        {"components": graph["components"], **content["application/json"]["schema"]}
    )
    assert validator.is_valid(response.json())
    client = _client(tmp_path, response)
    adopted = None
    try:
        adopted = client.request("GET", path)
    except ControlClientError:
        pass
    assert adopted is None
    fresh = _peer(snapshot).get("/api/fleet")
    client._opener = lambda _request, **_kwargs: ObservationHTTPPeer(fresh)
    assert client.request("GET", "/api/fleet")["nodes"]


def test_global_validation_error_bytes_match_streamed_route_and_cli_contract(tmp_path):
    snapshot = _large_snapshot(tmp_path)
    app, peer = _app_peer(snapshot)
    with peer:

        @app.get("/api/observation-validation-fixture")
        def validation_fixture(value: int) -> dict[str, int]:
            return {"value": value}

        # Exercise the real global validation renderer. Fleet/platform have no
        # query input to invalidate today, but inherit this exact 422 contract.
        response = peer.get("/api/observation-validation-fixture?value=not-an-integer")
        graph = app.openapi()
    assert response.status_code == 422
    assert response.headers["content-type"].partition(";")[0] == "application/json"
    schema = graph["paths"]["/api/platform"]["get"]["responses"]["422"]["content"][
        "application/json"
    ]["schema"]
    assert source_schema_validator(
        {"components": graph["components"], **schema}
    ).is_valid(response.json())
    client = _client(tmp_path, response)
    adopted = None
    try:
        adopted = client.request("GET", "/api/platform", timeout_seconds=0.01)
    except ControlClientError:
        pass
    assert adopted is None
    fresh = _peer(snapshot).get("/api/fleet")
    client._opener = lambda _request, **_kwargs: ObservationHTTPPeer(fresh)
    assert client.request("GET", "/api/fleet")["nodes"]


def test_platform_capture_failure_bytes_preserve_retry_through_cli(
    tmp_path, monkeypatch
):
    from vonk_control.api import application as api
    from vonk_control.platform_observation_errors import ObservationCaptureUnavailable

    def unreadable_capture():
        raise ObservationCaptureUnavailable(phase="stored-worker-validation")

    monkeypatch.setattr(api, "api_only_observation", unreadable_capture)
    snapshot = _large_snapshot(tmp_path)
    app, peer = _app_peer(snapshot)
    with peer:
        response = peer.get("/api/platform")
        graph = app.openapi()
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.headers["content-type"].partition(";")[0] == "application/json"
    content = graph["paths"]["/api/platform"]["get"]["responses"]["503"]["content"]
    assert set(content) == {"application/json"}
    assert source_schema_validator(
        {"components": graph["components"], **content["application/json"]["schema"]}
    ).is_valid(response.json())
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    client = _client(tmp_path, response)
    peers = []

    def opener(_request, **_kwargs):
        peer = ObservationHTTPPeer(response)
        peers.append(peer)
        return peer

    client._opener = opener
    adopted = None
    try:
        adopted = client.request("GET", "/api/platform")
    except ControlClientError:
        pass
    assert adopted is None and now[0] == 115.0
    assert len(peers) == 3 and all(peer._body.closed for peer in peers)
    monkeypatch.undo()
    fresh = _peer(snapshot).get("/api/fleet")
    client._opener = lambda _request, **_kwargs: ObservationHTTPPeer(fresh)
    assert client.request("GET", "/api/fleet")["nodes"]
