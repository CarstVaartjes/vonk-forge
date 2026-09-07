from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from vonk_agent_protocol import (
    AgentProtocolError,
    RecipeRunObservationGrantRequest,
    RecipeRunObservationsWire,
    RecipeRunObservationWire,
    canonical_message,
)


def _observation() -> dict[str, object]:
    fixture = Path(__file__).parents[1] / "fixtures" / "recipe-run-observation.json"
    return json.loads(fixture.read_text())


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
    with pytest.raises(AgentProtocolError):
        RecipeRunObservationsWire.parse(
            {"schema_version": 1, "observed_at": "2026-09-07T00:00:00Z", "runs": []}
        )
    payload = _observation()
    payload.pop("helper_receipt")
    with pytest.raises(AgentProtocolError):
        RecipeRunObservationWire.parse(payload)


def test_singleton_observation_uses_the_same_signed_structure() -> None:
    payload = _observation()
    payload.update(
        {
            "rank": 0,
            "role": "entrypoint",
            "world_size": 1,
            "local_address": None,
            "master_address": None,
            "master_port": None,
            "endpoint_ready": True,
        }
    )
    identity = {
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
    identity_sha256 = hashlib.sha256(canonical_message(identity)).hexdigest()
    payload["observation_identity_sha256"] = identity_sha256
    payload["grant"]["claims"]["operation"]["observation_identity_sha256"] = (
        identity_sha256  # type: ignore[index]
    )
    payload["helper_receipt"]["claims"]["observation_identity_sha256"] = identity_sha256  # type: ignore[index]
    parsed = RecipeRunObservationWire.parse(payload)
    assert parsed.world_size == 1
    assert parsed.endpoint_ready is True


def test_observation_endpoint_readiness_field_is_required() -> None:
    payload = _observation()
    payload.pop("endpoint_ready")
    with pytest.raises(AgentProtocolError):
        RecipeRunObservationWire.parse(payload)


def test_observation_grant_request_is_shared_and_preserves_singleton_nulls() -> None:
    payload = _observation()
    request = {
        key: payload[key]
        for key in (
            "schema_version",
            "node_id",
            "run_id",
            "installation_id",
            "recipe_revision_id",
            "recipe_content_sha256",
            "mapping_id",
            "mapping_generation",
            "run_generation",
            "image_digest",
            "artifact_set_digest",
            "model_identity",
            "rank",
            "role",
            "world_size",
            "local_address",
            "master_address",
            "master_port",
            "port",
            "runtime_arguments_sha256",
        )
    }
    request.update(
        {
            "job_id": payload["run_id"],
            "operation_id": "60000000-0000-4000-8000-000000000006",
            "attempt": 3,
            "fence": "70000000-0000-4000-8000-000000000007",
            "request_sha256": "e" * 64,
            "expires_in_seconds": 10,
        }
    )
    request.update(
        {
            "rank": 0,
            "role": "entrypoint",
            "world_size": 1,
            "local_address": None,
            "master_address": None,
            "master_port": None,
            "operation_id": "60000000-0000-4000-8000-000000000006",
            "attempt": 3,
        }
    )
    parsed = RecipeRunObservationGrantRequest.model_validate(request)
    assert parsed.local_address is None
    assert parsed.master_address is None
    assert parsed.master_port is None

    payload = _observation()
    payload["observation_receipt_public_key"] = "11" * 32
    with pytest.raises(AgentProtocolError):
        RecipeRunObservationWire.parse(payload)


@pytest.mark.parametrize(
    "field",
    ["local_address", "master_address", "master_port"],
)
def test_distributed_observation_rejects_partial_rendezvous(field: str) -> None:
    payload = _observation()
    payload[field] = None
    with pytest.raises(AgentProtocolError):
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
