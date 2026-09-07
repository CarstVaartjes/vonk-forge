from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from vonk_agent_protocol import (
    RecipeRunObservationWire,
    RecipeRunObservationsWire,
    canonical_message,
)


def _observation() -> dict[str, object]:
    identity: dict[str, object] = {
        "schema_version": 1,
        "node_id": "spk_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "run_id": "10000000-0000-4000-8000-000000000001",
        "installation_id": "20000000-0000-4000-8000-000000000002",
        "recipe_revision_id": "30000000-0000-4000-8000-000000000003",
        "recipe_content_sha256": "a" * 64,
        "mapping_id": "40000000-0000-4000-8000-000000000004",
        "mapping_generation": 2,
        "run_generation": 3,
        "image_digest": "b" * 64,
        "artifact_set_digest": "c" * 64,
        "model_identity": "publisher/model@revision",
        "rank": 1,
        "role": "worker",
        "world_size": 2,
        "local_address": "192.168.100.3",
        "master_address": "192.168.100.2",
        "master_port": 29500,
        "port": 8888,
        "runtime_arguments_sha256": "d" * 64,
    }
    observed_at = int(datetime(2026, 9, 7, tzinfo=UTC).timestamp())
    public_key = "00" * 32
    identity_sha256 = hashlib.sha256(canonical_message(identity)).hexdigest()
    return identity | {
        "observed_at": datetime.fromtimestamp(observed_at, UTC).isoformat(),
        "endpoint_ready": None,
        "observation_identity_sha256": identity_sha256,
        "grant": {"claims": {"request_id": "50000000-0000-4000-8000-000000000005"}},
        "helper_receipt": {
            "schema_version": 1,
            "claims": {
                "schema_version": 1,
                "authority": "vonk.recipe-run-observation-helper",
                "node_id": identity["node_id"],
                "request_id": "50000000-0000-4000-8000-000000000005",
                "request_sha256": "e" * 64,
                "observation_identity_sha256": identity_sha256,
                "outcome": "running",
                "observed_at": observed_at,
            },
            "signature": {
                "algorithm": "ed25519",
                "key_id": hashlib.sha256(bytes.fromhex(public_key)).hexdigest(),
                "value": "f" * 128,
            },
        },
        "observation_receipt_public_key": public_key,
    }


def test_rust_shaped_observation_round_trips_through_shared_wire_model() -> None:
    payload = _observation()
    parsed = RecipeRunObservationWire.parse(payload)
    assert parsed.observation_identity() == {
        key: value
        for key, value in payload.items()
        if key
        not in {
            "observed_at",
            "endpoint_ready",
            "observation_identity_sha256",
            "grant",
            "helper_receipt",
            "observation_receipt_public_key",
        }
    }
    envelope = RecipeRunObservationsWire.parse(
        {"schema_version": 2, "observed_at": payload["observed_at"], "runs": [payload]}
    )
    assert envelope.schema_version == 2


def test_legacy_and_receiptless_observations_are_rejected() -> None:
    with pytest.raises(Exception):
        RecipeRunObservationsWire.parse(
            {"schema_version": 1, "observed_at": "2026-09-07T00:00:00Z", "runs": []}
        )
    payload = _observation()
    payload.pop("helper_receipt")
    with pytest.raises(Exception):
        RecipeRunObservationWire.parse(payload)

    payload = _observation()
    payload["observation_receipt_public_key"] = "11" * 32
    with pytest.raises(Exception):
        RecipeRunObservationWire.parse(payload)


@pytest.mark.parametrize("version", [True, 2.0])
def test_observation_snapshot_schema_version_is_exact_integer(version: object) -> None:
    with pytest.raises(ValidationError):
        RecipeRunObservationsWire.model_validate_json(
            json.dumps(
                {
                    "schema_version": version,
                    "observed_at": "2026-09-07T00:00:00Z",
                    "runs": [],
                }
            )
        )
