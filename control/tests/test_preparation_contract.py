from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from vonk_control.preparation_contract import RolloutPreparation


NOW = datetime(2026, 9, 5, tzinfo=UTC)
NODE_A = "spk_" + "1" * 32
NODE_B = "spk_" + "2" * 32


def _ready_document() -> dict[str, object]:
    controller = {
        "state": "ready",
        "expected_bytes": 100,
        "verified_bytes": 100,
        "missing_bytes": 0,
        "verified_at": NOW,
        "source": "nas-cache",
    }
    targets = [
        {
            "node_id": node_id,
            "state": "ready",
            "expected_bytes": 100,
            "present_bytes": 100,
            "missing_bytes": 0,
            "verified_identity": "a" * 64,
            "verified_at": NOW,
        }
        for node_id in (NODE_A, NODE_B)
    ]
    image_targets = [{**target, "verified_identity": "sha256:" + "d" * 64, "imported": True} for target in targets]
    return {
        "schema_version": 2,
        "model": {
            "artifact_set_sha256": "a" * 64,
            "model_version_sha256": "b" * 64,
            "artifact_count": 3,
            "dependency_model_version_sha256": ["c" * 64],
            "completeness": "complete",
            "controller": controller,
            "targets": targets,
        },
        "runtime_image": {
            "image_digest": "sha256:" + "d" * 64,
            "oci_layout_sha256": "e" * 64,
            "image_bytes": 100,
            "architecture": "linux-arm64",
            "runtime_interface": "v1",
            "build_id": "build-1",
            "controller": {**controller, "source": "controller-build"},
            "targets": image_targets,
        },
        "exceptions": [],
        "target_node_ids": [NODE_A, NODE_B],
        "controller_ready": True,
        "targets_ready": True,
        "ready": True,
        "reasons": [],
    }


def test_ready_preparation_binds_model_image_and_complete_target_scope() -> None:
    value = RolloutPreparation.model_validate(_ready_document())

    assert value.schema_version == 2
    assert value.model.artifact_count == 3
    assert value.runtime_image.targets[1].imported is True
    assert value.ready is True


def test_cached_model_without_ready_image_is_not_controller_ready() -> None:
    document = _ready_document()
    image = document["runtime_image"]
    assert isinstance(image, dict)
    image["controller"] = {
        "state": "missing",
        "expected_bytes": 100,
        "verified_bytes": 0,
        "missing_bytes": 100,
        "source": "unknown",
    }
    document["controller_ready"] = False
    document["ready"] = False

    value = RolloutPreparation.model_validate(document)

    assert value.model.controller.state == "ready"
    assert value.runtime_image.controller.state == "missing"
    assert value.ready is False


def test_preparation_rejects_partial_scope_or_optimistic_readiness() -> None:
    partial = _ready_document()
    model = partial["model"]
    assert isinstance(model, dict)
    targets = model["targets"]
    assert isinstance(targets, list)
    targets.pop()
    with pytest.raises(ValidationError, match="complete target scope"):
        RolloutPreparation.model_validate(partial)

    optimistic = _ready_document()
    optimistic["ready"] = False
    with pytest.raises(ValidationError, match="rollout readiness"):
        RolloutPreparation.model_validate(optimistic)


def test_reusable_gpu_exception_requires_compatibility_artifact() -> None:
    document = _ready_document()
    document["exceptions"] = [
        {
            "kind": "engine-generation",
            "stage": "target-prepare",
            "compatibility_key": "model=b;sm=121;driver=580",
            "state": "ready",
            "reusable": True,
            "node_ids": [NODE_A, NODE_B],
        }
    ]
    with pytest.raises(ValidationError, match="artifact digest"):
        RolloutPreparation.model_validate(document)
