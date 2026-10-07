"""Export actual immutable-observation producer bytes for hosted browser proof."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from vonk_control.observation_transfer import (
    ObservationTransferResponse,
    observation_response,
)
from vonk_control.strict_json import serialize_json_value

from tests.test_observation_transfer import _large_snapshot


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

            async def collect(response: ObservationTransferResponse) -> str:
                pieces: list[bytes] = []
                async for part in response.body_iterator:
                    pieces.append(
                        part.encode() if isinstance(part, str) else bytes(part)
                    )
                return b"".join(pieces).decode("utf-8", errors="strict")

            records.append(
                {
                    "name": name,
                    "path": "/api/fleet",
                    "media_type": response.media_type,
                    "body": asyncio.run(collect(response)),
                    "payload_component": "FleetSnapshot",
                    "payload_json": expected,
                }
            )
    output.mkdir(parents=True, exist_ok=True)
    (output / "observation-transfers.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    export(parser.parse_args().output)
