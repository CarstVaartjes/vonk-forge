from __future__ import annotations

import hashlib
import json
import os
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ConfigDict, Field, JsonValue, RootModel
from vonk_agent_protocol import (
    AgentClaim,
    AgentProtocolError,
    canonical_message,
    canonical_payload,
)
from vonk_agent_protocol.host_helper import (
    HOST_HELPER_GRANT_DOMAIN,
    HostHelperGrantClaims,
    host_helper_grant_signing_bytes,
)
from vonk_agent_protocol.wire_model import OperationProgress, WireModel

VECTORS = Path(__file__).parents[1] / "src/vonk_agent_protocol/vectors"


class DisplayContract(WireModel):
    required_null: str | None
    optional_null: str | None = None
    enabled: bool = False
    count: int = 0
    entries: list[str] = Field(default_factory=list)
    engine: dict[str, JsonValue]


class NestedContract(WireModel):
    children: list[DisplayContract]


def test_canonical_models_keep_required_null_defaults_and_engine_values() -> None:
    engine = {
        "optional_null": None,
        "nested": {"required_null": None},
        "items": [None, 0, False, ""],
    }
    omitted = DisplayContract(required_null=None, engine=engine)
    explicit = DisplayContract(required_null=None, optional_null=None, engine=engine)
    expected = {
        "required_null": None,
        "enabled": False,
        "count": 0,
        "entries": [],
        "engine": engine,
    }
    assert json.loads(canonical_message(omitted)) == expected
    assert canonical_message(omitted) == canonical_message(explicit)
    assert json.loads(canonical_message(NestedContract(children=[explicit]))) == {
        "children": [expected]
    }
    assert json.loads(canonical_message(engine)) == engine


def job_document() -> dict:
    return json.loads((VECTORS / "recipe-job-run-claim-v1.json").read_text())


def test_real_job_payload_normalizes_optional_slots_before_hashing() -> None:
    claim = job_document()
    omitted = deepcopy(claim["payload"])
    omitted["compiled_execution_plan"]["job"]["input"].pop("slots", None)
    explicit = deepcopy(omitted)
    explicit["compiled_execution_plan"]["job"]["input"]["slots"] = None
    encoded = canonical_payload(claim["operation"], omitted)
    assert canonical_payload(claim["operation"], explicit) == encoded
    normalized = json.loads(encoded)
    assert "slots" not in normalized["compiled_execution_plan"]["job"]["input"]
    assert normalized["compiled_execution_plan"]["endpoint"] is None
    assert normalized["compiled_execution_plan"]["runtime"]["placement"]["port"] is None
    broken = deepcopy(omitted)
    del broken["compiled_execution_plan"]["endpoint"]
    with pytest.raises(AgentProtocolError):
        canonical_payload(claim["operation"], broken)
    claim.update(payload=normalized, payload_digest=hashlib.sha256(encoded).hexdigest())
    assert (
        AgentClaim.model_validate(claim).payload_digest
        == hashlib.sha256(encoded).hexdigest()
    )


def probe(model: str, document: bytes) -> bytes:
    executable = os.environ.get("VONK_CANONICAL_WIRE_PROBE")
    if not executable:
        pytest.skip(
            "VONK_CANONICAL_WIRE_PROBE is required for the connected Rust boundary"
        )
    return subprocess.run(
        [executable, model], input=document, capture_output=True, check=True
    ).stdout


def test_rust_and_python_canonicalize_actual_job_claim_and_build_identically() -> None:
    claim = job_document()
    claim["payload"]["compiled_execution_plan"]["job"]["input"]["slots"] = None
    payload = canonical_payload(claim["operation"], claim["payload"])
    claim["payload_digest"] = hashlib.sha256(payload).hexdigest()
    encoded = canonical_message(AgentClaim.model_validate(claim))
    rust_claim = probe("AgentClaim", canonical_message(claim))
    assert rust_claim == encoded
    assert canonical_message(json.loads(rust_claim)["payload"]) == payload
    assert probe("RecipeJobRunRequest", canonical_message(claim["payload"])) == payload
    build = json.loads((VECTORS / "recipe-build-claim-v1.json").read_text())[
        "base_payload"
    ]
    build["target"] = None
    build["options"].update(ignorefile=None, os_version=None, timestamp=None)
    assert probe("RecipeBuildRequest", canonical_message(build)) == canonical_payload(
        "recipe.build.v1", build
    )


def test_progress_defaults_have_connected_canonical_parity() -> None:
    value = {"phase": "transfer", "checkpoint": None, "members": []}
    expected = b'{"completed_bytes":0,"members":[],"phase":"transfer","total_bytes_known":false}'
    assert canonical_message(OperationProgress.model_validate(value)) == expected
    assert probe("OperationProgress", canonical_message(value)) == expected


def test_host_grant_signing_bytes_use_the_same_optional_null_policy() -> None:
    value = {
        "schema_version": 1,
        "authority": "vonk.host-maintenance-helper",
        "request_id": "10000000-0000-4000-8000-000000000001",
        "node_id": "spk_" + "a" * 32,
        "issued_at": 2100000000,
        "expires_at": 2100000060,
        "operation": {
            "type": "execute-container-runtime-request",
            "action": "start",
            "job_id": "20000000-0000-4000-8000-000000000002",
            "operation_id": "30000000-0000-4000-8000-000000000003",
            "attempt": 1,
            "fence": "40000000-0000-4000-8000-000000000004",
            "request_sha256": "a" * 64,
        },
    }
    omitted = HostHelperGrantClaims.model_validate(value)
    value["operation"]["observation_identity_sha256"] = None
    explicit = HostHelperGrantClaims.model_validate(value)
    assert host_helper_grant_signing_bytes(omitted) == host_helper_grant_signing_bytes(
        explicit
    )
    assert host_helper_grant_signing_bytes(
        explicit
    ) == HOST_HELPER_GRANT_DOMAIN + canonical_message(explicit)


def test_canonical_root_list_and_serialization_aliases() -> None:
    class AliasedContract(WireModel):
        model_config = ConfigDict(serialize_by_alias=True)
        optional: str | None = Field(default=None, serialization_alias="optionalWire")
        required: str | None = Field(serialization_alias="requiredWire")

    value = AliasedContract(required=None)
    assert json.loads(canonical_message(RootModel[list[AliasedContract]]([value]))) == [
        {"requiredWire": None}
    ]
    assert canonical_message(RootModel[str]("unchanged")) == b'"unchanged"'
