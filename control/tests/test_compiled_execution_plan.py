from __future__ import annotations

import copy
import hashlib
import json

import pytest
from pydantic import ValidationError
from vonk_control.compiled_execution_plan import (
    CompiledExecutionPlan,
    CompiledExecutionPlanError,
    CompiledModelArtifact,
    compile_verified_execution_plan,
)


def _spec(
    *, recipe_digest: str = "a" * 64, mount_target: str = "/models"
) -> dict[str, object]:
    return {
        "identity": {
            "recipe_revision_sha256": recipe_digest,
            "harness_sha256": "b" * 64,
            "execution_sha256": "c" * 64,
        },
        "model_artifact_set_sha256": "d" * 64,
        "artifacts": [
            {
                "id": "weights",
                "selection_id": "primary",
                "file_id": "weights",
                "path": "model.safetensors",
                "roles": ["entrypoint", "weights"],
                "mount": {
                    "source": "/run/vonk/models/primary/weights",
                    "target": mount_target,
                    "read_only": True,
                },
                "model": {
                    "publisher": "vonk-forge",
                    "slug": "synthetic-tiny-fp16",
                    "content_sha256": "e" * 64,
                },
            }
        ],
    }


def _model_objects() -> list[dict[str, object]]:
    payload = b"verified model bytes"
    return [
        {
            "name": "model.safetensors",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
            "kind": "model",
        }
    ]


def _image(
    *, source: str = "published", build_id: str | None = None
) -> dict[str, object]:
    layout = "f" * 64
    return {
        "image_digest": "sha256:" + "1" * 64,
        "oci_layout_sha256": layout,
        "image_bytes": 4096,
        "architecture": "linux-arm64",
        "runtime_interface": "vonk.runtime.v1",
        "source": source,
        "build_id": build_id,
        "distribution_object": {
            "name": "image.oci.tar",
            "sha256": layout,
            "bytes": 4096,
            "kind": "oci-archive",
        },
    }


def _compile(
    spec: dict[str, object] | None = None,
    *,
    image: dict[str, object] | None = None,
) -> CompiledExecutionPlan:
    return compile_verified_execution_plan(
        _spec() if spec is None else spec,
        model_artifact_set_sha256="d" * 64,
        model_objects=_model_objects(),
        runtime_image=_image() if image is None else image,
    )


def test_prebuilt_plan_binds_exact_file_and_controller_archive_receipts() -> None:
    plan = _compile()

    artifact = plan.artifacts[0]
    assert artifact.sha256 == _model_objects()[0]["sha256"]
    assert artifact.bytes == len(b"verified model bytes")
    assert artifact.distribution_object.name == "model.safetensors"
    assert artifact.mount.source == "/run/vonk/models/primary/weights"
    assert artifact.roles == ["entrypoint", "weights"]
    assert plan.runtime_image.source == "published"
    assert plan.runtime_image.distribution_object.kind == "oci-archive"

    payload = plan.to_agent_payload()
    rendered = json.dumps(payload, sort_keys=True)
    assert "repository" not in rendered
    assert "revision" not in rendered
    assert "token" not in rendered
    assert "recipe_revision_sha256" not in payload
    assert "source" not in payload["runtime_image"]
    assert "build_id" not in payload["runtime_image"]


def test_controller_built_receipt_and_pulled_receipt_share_reusable_identity() -> None:
    prebuilt = _compile()
    built = _compile(image=_image(source="controller-build", build_id="build-7"))
    assert built.runtime_image.build_id == "build-7"
    assert built.reusable_identity_sha256 == prebuilt.reusable_identity_sha256

    editorial = _compile(_spec(recipe_digest="9" * 64))
    assert editorial.recipe_revision_sha256 != prebuilt.recipe_revision_sha256
    assert editorial.reusable_identity_sha256 == prebuilt.reusable_identity_sha256


def test_mount_change_invalidates_reuse_identity_without_changing_bytes() -> None:
    first = _compile()
    changed = _compile(_spec(mount_target="/models/alternate"))

    assert changed.artifacts[0].sha256 == first.artifacts[0].sha256
    assert changed.artifacts[0].bytes == first.artifacts[0].bytes
    assert changed.reusable_identity_sha256 != first.reusable_identity_sha256


def test_selector_label_change_does_not_invalidate_reusable_bytes() -> None:
    first = _compile()
    changed_spec = _spec()
    changed_spec["artifacts"][0]["id"] = "release-label"

    changed = _compile(changed_spec)
    assert changed.artifacts[0].id == "release-label"
    assert changed.reusable_identity_sha256 == first.reusable_identity_sha256


def test_upstream_authority_cannot_enter_compiled_receipts() -> None:
    polluted = _spec()
    model = polluted["artifacts"][0]["model"]
    assert isinstance(model, dict)
    model["repository"] = "huggingface.co/private/model"

    with pytest.raises(CompiledExecutionPlanError, match="upstream authority"):
        _compile(polluted)


def test_retired_runtime_authority_cannot_enter_compiled_receipts() -> None:
    polluted = _spec()
    polluted["identity"]["model_version_sha256"] = "f" * 64

    with pytest.raises(CompiledExecutionPlanError, match="retired authority"):
        _compile(polluted)


def test_mismatched_distribution_receipt_is_rejected() -> None:
    plan = _compile()
    artifact = plan.artifacts[0].model_dump(mode="json")
    artifact["distribution_object"]["bytes"] += 1

    with pytest.raises(ValidationError, match="bytes do not match"):
        CompiledModelArtifact.model_validate(artifact)


def test_controller_build_requires_build_id_and_exact_archive_identity() -> None:
    with pytest.raises(CompiledExecutionPlanError, match="verified runtime image"):
        _compile(image=_image(source="controller-build"))

    image = _image()
    image["distribution_object"]["sha256"] = "2" * 64
    with pytest.raises(CompiledExecutionPlanError, match="verified runtime image"):
        _compile(image=image)


def test_plan_rejects_incomplete_selected_cache_receipt() -> None:
    objects = _model_objects()
    objects[0]["name"] = "config.json"
    with pytest.raises(CompiledExecutionPlanError, match="not covered"):
        compile_verified_execution_plan(
            _spec(),
            model_artifact_set_sha256="d" * 64,
            model_objects=objects,
            runtime_image=_image(),
        )


def test_cache_authority_digest_is_explicit_when_runtime_spec_omits_it() -> None:
    spec = _spec()
    spec.pop("model_artifact_set_sha256")

    plan = compile_verified_execution_plan(
        spec,
        model_artifact_set_sha256="d" * 64,
        model_objects=_model_objects(),
        runtime_image=_image(),
    )
    assert plan.model_artifact_set_sha256 == "d" * 64


def test_runtime_spec_cannot_disagree_with_cache_authority_digest() -> None:
    with pytest.raises(CompiledExecutionPlanError, match="does not match"):
        compile_verified_execution_plan(
            _spec(),
            model_artifact_set_sha256="1" * 64,
            model_objects=_model_objects(),
            runtime_image=_image(),
        )


def test_plan_identity_does_not_mutate_canonical_runtime_input() -> None:
    spec = _spec()
    original = copy.deepcopy(spec)
    _compile(spec)
    assert spec == original
