"""Source-bound acceptance decoders use real canonical producers and receipts."""

from __future__ import annotations

import io
import json
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NoReturn

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.api import create_app
from vonk_control.auth import Actor, TokenCodec
from vonk_control.fleet_projection import FleetProjection, FleetSnapshot
from vonk_control.models import Base
from vonk_control.strict_json import serialize_json_value

from tests.acceptance.controller_contract import ContractSkew, ControllerContract
from tests.acceptance.test_spark_lifecycle import LifecycleError, LocalBrowserController

ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_SOURCE = "82f7e265d837de0ab37072a48bc8f707fa3bb181"


class Jobs:
    def enqueue(self, *_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("observation cannot enqueue")

    def get(self, job_id: str) -> NoReturn:
        raise KeyError(job_id)


class Projection:
    def __init__(self, snapshot: FleetSnapshot) -> None:
        self.snapshot = snapshot

    def read(self) -> FleetSnapshot:
        return self.snapshot


def test_verified_source_selects_transport_and_refuses_partial_then_recovers(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'fleet.sqlite'}")
    Base.metadata.create_all(engine)
    snapshot = FleetProjection(
        sessionmaker(engine), clock=lambda: datetime(2026, 10, 7, tzinfo=UTC)
    ).read()
    engine.dispose()
    codec = TokenCodec(b"k" * 32)
    app = create_app(
        jobs=Jobs(), tokens=codec, now=lambda: 10, fleet_projection=Projection(snapshot)
    )
    token = codec.issue(Actor("viewer", "viewer"), ttl_seconds=100, now=0)
    current = ControllerContract(app.openapi(), label="actual current source")
    selected = current.observation("/api/fleet")
    with TestClient(app) as peer:
        response = peer.get("/api/fleet", headers={"Authorization": f"Bearer {token}"})
    expected = serialize_json_value(snapshot)
    assert selected.media_type == "application/x-vonk-observation+ndjson"
    assert (
        selected.decode(
            io.BytesIO(response.content),
            status=200,
            media_type=response.headers["content-type"],
            deadline=time.monotonic() + 10,
        )
        == expected
    )
    damaged = b"".join(response.content.splitlines(keepends=True)[:-1])
    with pytest.raises(ValueError, match="without a complete final receipt"):
        selected.decode(
            io.BytesIO(damaged),
            status=200,
            media_type=selected.media_type,
            deadline=time.monotonic() + 10,
        )
    assert (
        selected.decode(
            io.BytesIO(response.content),
            status=200,
            media_type=selected.media_type,
            deadline=time.monotonic() + 10,
        )
        == expected
    )
    # Receipt equality alone cannot distinguish 0 from the JSON float token
    # 0.0. The actual source validator must reject each integral float counter.
    for kind, field in (
        ("chunk", "ordinal"),
        ("complete", "chunks"),
        ("complete", "bytes"),
    ):
        records = [json.loads(line) for line in response.content.splitlines()]
        record = next(record for record in records if record["type"] == kind)
        record[field] = float(record[field])
        noncanonical = b"".join(
            json.dumps(record).encode() + b"\n" for record in records
        )
        with pytest.raises(ContractSkew, match="source schema"):
            selected.decode(
                io.BytesIO(noncanonical),
                status=200,
                media_type=selected.media_type,
                deadline=time.monotonic() + 10,
            )
    assert (
        selected.decode(
            io.BytesIO(response.content),
            status=200,
            media_type=selected.media_type,
            deadline=time.monotonic() + 10,
        )
        == expected
    )
    with pytest.raises(ContractSkew, match="status or media differs"):
        selected.decode(
            io.BytesIO(json.dumps(expected).encode()),
            status=200,
            media_type="application/json",
            deadline=time.monotonic() + 10,
        )

    # This is a mechanically extracted route + transitive schema closure from
    # the actual historical source, not today's DTO pretending to be history.
    historical_document = json.loads(
        (ROOT / "tests/fixtures/controller-observation-json-82f7e265.json").read_text()
    )
    assert historical_document["x-vonk-fixture-source-sha"] == HISTORICAL_SOURCE
    historical = ControllerContract(historical_document, label=HISTORICAL_SOURCE)
    old = historical.observation("/api/fleet")
    assert old.media_type == "application/json"
    assert (
        old.decode(
            io.BytesIO(json.dumps(expected).encode()),
            status=200,
            media_type="application/json",
            deadline=time.monotonic() + 10,
        )
        == expected
    )
    with pytest.raises(ContractSkew, match="status or media differs"):
        old.decode(
            io.BytesIO(response.content),
            status=200,
            media_type=selected.media_type,
            deadline=time.monotonic() + 10,
        )
    with pytest.raises(ContractSkew, match="source schema"):
        old.decode(
            io.BytesIO(b'{"nodes":[]}'),
            status=200,
            media_type="application/json",
            deadline=time.monotonic() + 10,
        )
    with pytest.raises(ContractSkew, match="no GET"):
        historical.observation("/api/platform")


def test_local_acceptance_bearer_keeps_authorization_and_retries_verified_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Ambient proxy settings must never redirect loopback administrator traffic.
    for name in (
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "")
    engine = create_engine(f"sqlite:///{tmp_path / 'http-fleet.sqlite'}")
    Base.metadata.create_all(engine)
    snapshot = FleetProjection(
        sessionmaker(engine), clock=lambda: datetime(2026, 10, 7, tzinfo=UTC)
    ).read()
    engine.dispose()
    codec = TokenCodec(b"k" * 32)
    app = create_app(
        jobs=Jobs(), tokens=codec, now=lambda: 10, fleet_projection=Projection(snapshot)
    )
    contract = ControllerContract(app.openapi(), label="actual HTTP Controller source")
    token = codec.issue(Actor("viewer", "viewer"), ttl_seconds=100, now=0)
    observed_headers: list[dict[str, str]] = []
    corrupt = [True]
    with TestClient(app) as peer:

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                headers = dict(self.headers.items())
                observed_headers.append(headers)
                response = peer.get(self.path, headers=headers)
                body = response.content
                if response.status_code == 200 and corrupt[0]:
                    body = b"".join(body.splitlines(keepends=True)[:-1])
                    corrupt[0] = False
                self.send_response(response.status_code)
                self.send_header("Content-Type", response.headers["content-type"])
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            boundary = LocalBrowserController(
                hostname="controller.test",
                port=server.server_port,
                observation_contract=lambda: contract,
            )
            client = boundary.bearer(token, timeout=5)
            with pytest.raises(
                LifecycleError, match="complete source-bound observation"
            ):
                client.request("GET", "/api/fleet")
            assert client.request("GET", "/api/fleet") == (
                200,
                serialize_json_value(snapshot),
            )
            with pytest.raises(
                LifecycleError, match="complete source-bound observation"
            ):
                boundary.bearer("invalid-token", timeout=5).request("GET", "/api/fleet")
            assert len(observed_headers) == 3
            assert all(
                headers["Host"] == "controller.test" for headers in observed_headers
            )
            assert all(
                headers["Accept"] == "application/x-vonk-observation+ndjson"
                for headers in observed_headers
            )
            assert [headers["Authorization"] for headers in observed_headers] == [
                "Bearer " + token,
                "Bearer " + token,
                "Bearer invalid-token",
            ]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
