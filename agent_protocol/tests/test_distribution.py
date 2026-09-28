from __future__ import annotations

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


@pytest.mark.parametrize(
    "name",
    [
        "__init__.py",
        "weights/model bin",
        "UPPERCASE.bin",
        "模型.bin",
        "模型 file_" * 64,
    ],
)
def test_distribution_preserves_safe_object_names(name: str) -> None:
    wire = _assignment().to_mapping()
    wire["objects"][0]["name"] = name
    assert DistributionAssignment.parse(wire).objects[0].name == name


def test_distribution_assignment_carries_only_the_object_set() -> None:
    wire = _assignment().to_mapping()
    with pytest.raises(AgentProtocolError):
        DistributionAssignment.parse(wire | {"node_id": "spk_" + "b" * 32})


def test_compiled_distribution_reuses_the_assignment_object_contract() -> None:
    from vonk_agent_protocol.compiled_execution_plan import CompiledDistributionObject

    assert issubclass(CompiledDistributionObject, DistributionObject)
    wire = _assignment().objects[0].to_mapping()
    for name in ["weights/model bin", "__init__.py"]:
        wire["name"] = name
        assert CompiledDistributionObject.model_validate(wire).to_mapping() == wire
