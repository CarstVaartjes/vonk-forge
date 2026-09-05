from datetime import UTC, datetime
import hashlib
import json

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
        "verified_sha256": "a" * 64,
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
            "verified_sha256": "a" * 64,
            "verified_at": NOW,
        }
        for node_id in (NODE_A, NODE_B)
    ]
    image_targets = [
        {
            **target,
            "verified_sha256": "e" * 64,
            "imported_image_digest": "sha256:" + "d" * 64,
        }
        for target in targets
    ]
    return {
        "schema_version": 2,
        "model": {
            "artifact_set_sha256": "a" * 64,
            "model_version_sha256": "b" * 64,
            "artifact_count": 3,
            "artifact_set_bytes": 100,
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
            "controller": {
                **controller,
                "source": "controller-build",
                "verified_sha256": "e" * 64,
            },
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
    assert value.runtime_image.targets[1].imported_image_digest == "sha256:" + "d" * 64
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
    model = document["model"]
    assert isinstance(model, dict)
    model["recipe_revision_sha256"] = "f" * 64
    compatibility = {
        "recipe_revision_sha256": "f" * 64,
        "model_version_sha256": "b" * 64,
        "runtime_image_digest": "sha256:" + "d" * 64,
        "parameters_sha256": "9" * 64,
        "hardware_profile_sha256": "8" * 64,
    }
    compatibility_key = hashlib.sha256(
        json.dumps(compatibility, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    document["exceptions"] = [
        {
            "kind": "engine-generation",
            "stage": "target-prepare",
            "compatibility": compatibility,
            "compatibility_key_sha256": compatibility_key,
            "state": "ready",
            "reusable": True,
            "node_ids": [NODE_A, NODE_B],
        }
    ]
    with pytest.raises(ValidationError, match="artifact digest"):
        RolloutPreparation.model_validate(document)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda value: value["model"]["controller"].update(
                verified_sha256="7" * 64
            ),
            "Controller model digest",
        ),
        (
            lambda value: value["model"]["targets"][0].update(
                verified_sha256="7" * 64
            ),
            "target model digest",
        ),
        (
            lambda value: value["runtime_image"]["controller"].update(
                verified_sha256="7" * 64
            ),
            "Controller image digest",
        ),
        (
            lambda value: value["runtime_image"]["targets"][0].update(
                imported_image_digest="sha256:" + "7" * 64
            ),
            "exact OCI layout and imported image",
        ),
    ],
)
def test_ready_preparation_rejects_wrong_verified_identity(mutate, message: str) -> None:
    document = _ready_document()
    mutate(document)
    with pytest.raises(ValidationError, match=message):
        RolloutPreparation.model_validate(document)


def test_exception_identity_and_scope_must_match_rollout_authority() -> None:
    document = _ready_document()
    model = document["model"]
    assert isinstance(model, dict)
    model["recipe_revision_sha256"] = "f" * 64
    compatibility = {
        "recipe_revision_sha256": "f" * 64,
        "model_version_sha256": "7" * 64,
        "runtime_image_digest": "sha256:" + "d" * 64,
        "parameters_sha256": "9" * 64,
        "hardware_profile_sha256": None,
    }
    document["exceptions"] = [
        {
            "kind": "jit",
            "stage": "target-prepare",
            "compatibility": compatibility,
            "compatibility_key_sha256": hashlib.sha256(
                json.dumps(
                    compatibility, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest(),
            "state": "ready",
            "reusable": True,
            "node_ids": [NODE_A],
            "artifact_sha256": "6" * 64,
        }
    ]
    with pytest.raises(ValidationError, match="does not match the rollout authority"):
        RolloutPreparation.model_validate(document)

    out_of_scope = _ready_document()
    model = out_of_scope["model"]
    assert isinstance(model, dict)
    model["recipe_revision_sha256"] = "f" * 64
    compatibility["model_version_sha256"] = "b" * 64
    out_of_scope["exceptions"] = [
        {
            **document["exceptions"][0],
            "compatibility": compatibility,
            "compatibility_key_sha256": hashlib.sha256(
                json.dumps(
                    compatibility, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest(),
            "node_ids": ["spk_" + "3" * 32],
        }
    ]
    with pytest.raises(ValidationError, match="exceeds the rollout target scope"):
        RolloutPreparation.model_validate(out_of_scope)
