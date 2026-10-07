"""Export actual immutable-observation producer bytes for hosted browser proof."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import update
from vonk_control.fleet_projection import FleetProjection
from vonk_control.fleet_stream import FleetStream
from vonk_control.models import AgentNode, FleetStreamEvent
from vonk_control.observation_transfer import (
    ObservationTransferResponse,
    observation_response,
)
from vonk_control.strict_json import serialize_json_value
from vonk_control.telemetry import TelemetryRepository

from cluster_profiles.control_limits import MAX_CONTROL_DOCUMENT_BYTES
from tests.test_fleet_stream import (
    NODE_ID,
    NOW,
    _events,
    _operation_draft,
    _production_stream_store,
)
from tests.test_observation_transfer import _large_snapshot, _peer


async def _collect(response: ObservationTransferResponse) -> str:
    pieces: list[bytes] = []
    async for part in response.body_iterator:
        pieces.append(part.encode() if isinstance(part, str) else bytes(part))
    return b"".join(pieces).decode("utf-8", errors="strict")


def _export_saved_event_recovery(output: Path) -> None:
    engine, sessions, repository = _production_stream_store()
    try:
        with sessions.begin() as session:
            session.add(AgentNode(node_id=NODE_ID, state="active", last_seen_at=NOW))
            event = repository.append_in_session(session, _operation_draft(1))
            payload = _operation_draft(1).payload.model_dump(mode="json")
            # Healthy writes are bounded at 8KiB. This models damage to optional
            # saved outbox decoration, preserving the actual event identity.
            payload["kind"] = "x" * (MAX_CONTROL_DOCUMENT_BYTES + 1)
            session.execute(
                update(FleetStreamEvent)
                .where(FleetStreamEvent.id == event.id)
                .values(payload=payload)
            )
        stream = FleetStream(
            repository, TelemetryRepository(sessions, clock=lambda: NOW)
        )

        async def first_frame() -> str:
            generator = _events(stream, 0)
            try:
                return await anext(generator)
            finally:
                await generator.aclose()

        notice = asyncio.run(first_frame())
        snapshot = FleetProjection(
            sessions, events=repository, clock=lambda: NOW
        ).read()
        assert snapshot.event_cursor == event.id
        assert snapshot.nodes[0].id == NODE_ID
        response = observation_response(snapshot, resource="fleet")
        body = asyncio.run(_collect(response))
        with sessions.begin() as session:
            session.execute(
                update(FleetStreamEvent)
                .where(FleetStreamEvent.id == event.id)
                .values(payload=_operation_draft(1).payload.model_dump(mode="json"))
            )
        repaired_frame = asyncio.run(first_frame())
        document = {
            "notice": notice,
            "repaired_frame": repaired_frame,
            "event_cursor": event.id,
            "path": "/api/fleet",
            "media_type": response.media_type,
            "body": body,
            "payload_json": json.dumps(
                serialize_json_value(snapshot),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
        (output / "sparse-event-recovery.json").write_text(
            json.dumps(document, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    finally:
        engine.dispose()


def _export_response_errors(output: Path) -> None:
    from vonk_control.platform_observation_errors import ObservationCaptureUnavailable

    records: list[dict[str, object]] = []
    with (
        tempfile.TemporaryDirectory() as directory,
        _peer(_large_snapshot(Path(directory))) as peer,
    ):
        for path in ("/api/fleet", "/api/platform", "/api/fleet/stream"):
            response = peer.get(path, headers={"Authorization": "Bearer invalid"})
            assert response.status_code == 401
            records.append(
                {
                    "path": path,
                    "status": response.status_code,
                    "media_type": response.headers["content-type"],
                    "body": response.text,
                }
            )

        @peer.app.get("/api/observation-validation-fixture")
        def validation_fixture(value: int) -> dict[str, int]:
            return {"value": value}

        # The real global validation handler produces these bytes. The
        # observation GET routes currently have no invalidatable query.
        response = peer.get("/api/observation-validation-fixture?value=not-an-integer")
        assert response.status_code == 422
        records.append(
            {
                "path": "/api/platform",
                "status": response.status_code,
                "media_type": response.headers["content-type"],
                "body": response.text,
            }
        )
        with patch(
            "vonk_control.api.api_only_observation",
            side_effect=ObservationCaptureUnavailable(phase="stored-worker-validation"),
        ):
            response = peer.get("/api/platform")
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "5"
        records.append(
            {
                "path": "/api/platform",
                "status": response.status_code,
                "media_type": response.headers["content-type"],
                "body": response.text,
                "retry_after": response.headers["Retry-After"],
            }
        )
    (output / "observation-response-errors.json").write_text(
        json.dumps(records, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def export(output: Path) -> None:
    records: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory() as directory:
        snapshot = _large_snapshot(Path(directory))
        # This is the authoritative legal BIGINT boundary, not a rounded JS
        # number manufactured by the fixture consumer.
        snapshot.event_cursor = 2**63 - 1
        for name in ("large-indivisible-fleet", "small-fleet"):
            if name == "small-fleet":
                snapshot.nodes[0].installed.clear()
            expected = json.dumps(
                serialize_json_value(snapshot),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            response = observation_response(snapshot, resource="fleet")

            records.append(
                {
                    "name": name,
                    "path": "/api/fleet",
                    "media_type": response.media_type,
                    "body": asyncio.run(_collect(response)),
                    "payload_component": "FleetSnapshot",
                    "payload_json": expected,
                }
            )
    output.mkdir(parents=True, exist_ok=True)
    (output / "observation-transfers.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    _export_saved_event_recovery(output)
    _export_response_errors(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    export(parser.parse_args().output)
