from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

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
        "grant": {
            "schema_version": 1,
            "claims": {
                "schema_version": 1,
                "authority": "vonk.host-maintenance-helper",
                "request_id": "50000000-0000-4000-8000-000000000005",
                "node_id": identity["node_id"],
                "issued_at": observed_at,
                "expires_at": observed_at + 60,
                "operation": {
                    "type": "execute-container-runtime-request",
                    "action": "run-inspect",
                    "job_id": identity["run_id"],
                    "operation_id": "60000000-0000-4000-8000-000000000006",
                    "attempt": 3,
                    "fence": "70000000-0000-4000-8000-000000000007",
                    "request_sha256": "e" * 64,
                    "observation_identity_sha256": identity_sha256,
                },
            },
            "signature": {
                "algorithm": "ed25519",
                "key_id": hashlib.sha256(bytes.fromhex(public_key)).hexdigest(),
                "value": "f" * 128,
            },
        },
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
