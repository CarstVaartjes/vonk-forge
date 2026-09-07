from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import BaseModel
from vonk_agent_protocol import (
    AgentProtocolError,
    DistributionAssignment,
    DistributionObject,
    canonical_message,
)


def _assignment() -> DistributionAssignment:
    return DistributionAssignment.parse(
        {
            "schema_version": 2,
            "assignment_id": str(uuid4()),
            "plan_digest": "a" * 64,
            "generation": 3,
            "node_id": "spk_" + "b" * 32,
            "expires_at": datetime(2026, 9, 5, 12, tzinfo=UTC).isoformat(),
            "model_artifact_set_sha256": "c" * 64,
            "objects": [
                {
                    "name": "weights/model.bin",
                    "sha256": "d" * 64,
                    "bytes": 13,
                    "kind": "model",
                },
                {
                    "name": "image.oci.tar",
                    "sha256": "e" * 64,
                    "bytes": 11,
                    "kind": "oci-archive",
                },
            ],
            "oci_image_digest": "sha256:" + "f" * 64,
            "oci_archive_sha256": "e" * 64,
        }
    )


def test_distribution_assignment_import_and_round_trip_are_canonical() -> None:
    assignment = _assignment()
    wire = assignment.to_mapping()

    assert isinstance(assignment, BaseModel)
    assert isinstance(assignment.objects[0], DistributionObject)
    assert DistributionAssignment.parse(wire).to_mapping() == wire
    assert canonical_message(assignment) == canonical_message(wire)


def test_distribution_assignment_rejects_unsafe_object_name() -> None:
    wire = _assignment().to_mapping()
    wire["objects"][0]["name"] = "weights/../model.bin"

    with pytest.raises(AgentProtocolError):
        DistributionAssignment.parse(wire)


def test_distribution_allows_only_the_canonical_empty_model_support_file() -> None:
    empty_sha256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    accepted = DistributionObject.parse(
        {
            "name": "__init__.py",
            "sha256": empty_sha256,
            "bytes": 0,
            "kind": "model",
        }
    )
    assert accepted.bytes == 0

    with pytest.raises(AgentProtocolError):
        DistributionObject.parse(
            {
                "name": "empty.oci.tar",
                "sha256": empty_sha256,
                "bytes": 0,
                "kind": "oci-archive",
            }
        )


@pytest.mark.parametrize("name", ["__init__.py", "weights/model bin", "UPPERCASE.bin", "模型.bin"])
def test_distribution_preserves_safe_object_names(name: str) -> None:
    wire = _assignment().to_mapping()
    wire["objects"][0]["name"] = name
    assert DistributionAssignment.parse(wire).objects[0].name == name


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2.0), ("schema_version", True),
        ("generation", True), ("generation", 3.0),
        ("expires_at", "2026-09-05T12:00:00"),
        ("expires_at", "2026-09-05T12:00:00+02:00"),
    ],
)
def test_distribution_rejects_wrong_json_types_and_non_utc_expiry(field: str, value: object) -> None:
    wire = _assignment().to_mapping()
    wire[field] = value
    with pytest.raises(AgentProtocolError):
        DistributionAssignment.parse(wire)


def test_compiled_distribution_reuses_the_assignment_object_contract() -> None:
    from vonk_agent_protocol.compiled_execution_plan import CompiledDistributionObject

    assert issubclass(CompiledDistributionObject, DistributionObject)
    wire = _assignment().objects[0].to_mapping()
    for name in ["weights/model bin", "__init__.py"]:
        wire["name"] = name
        assert CompiledDistributionObject.model_validate(wire).to_mapping() == wire
