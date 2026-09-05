from __future__ import annotations

import copy
import hashlib
import json

import pytest
from pydantic import ValidationError
from vonk_control.compiled_execution_plan import (
    EMPTY_SHA256,
    CompiledExecutionPlan,
    CompiledExecutionPlanError,
    CompiledModelArtifact,
    DistributionObjectReceipt,
    compile_verified_execution_plan,
    execution_identity_sha256,
    materialized_model_path,
)


def _spec(
    *, recipe_digest: str = "a" * 64, mount_target: str = "/models"
) -> dict[str, object]:
    payload = b"verified model bytes"
    spec: dict[str, object] = {
        "identity": {
            "recipe_revision_sha256": recipe_digest,
            "harness_sha256": "b" * 64,
            "execution_sha256": "0" * 64,
        },
        "model_artifact_set_sha256": "d" * 64,
        "runtime": {
            "interface": "vonk.runtime.v1",
            "adapter": "vllm",
            "adapter_version": 1,
            "image": "registry.example/vonk/vllm@sha256:" + "1" * 64,
            "architecture": "linux/arm64",
            "entrypoint": ["/opt/vonk/bin/vllm", "serve"],
            "arguments": [],
            "environment": [],
            "writable_paths": [],
        },
        "security": {
            "devices": [],
            "capabilities": [],
            "host_network": False,
            "privileged": False,
            "user": "10001:10001",
            "mounts": [
                {
                    "source": "/run/vonk/models",
                    "target": "/models",
                    "read_only": True,
                }
            ],
            "read_only_root": True,
            "no_new_privileges": True,
        },
        "lifecycle": {
            "pre_start": [],
            "post_stop": [],
            "stop_timeout_seconds": 30,
        },
        "topology": {
            "name": "solo",
            "mode": "single",
            "node_count": 1,
            "world_size": 1,
            "rank": 0,
            "role": "entrypoint",
            "backend": "local",
        },
        "endpoint": {
            "protocol": "openai",
            "port": 8000,
            "model_aliases": ["synthetic-tiny"],
            "health_path": "/v1/models",
        },
        "model_dependencies": [
            {
                "selection_id": "primary",
                "publisher": "vonk-forge",
                "slug": "synthetic-tiny-fp16",
                "content_sha256": "e" * 64,
                "artifact_key": "catalog-provenance-only",
            }
        ],
        "artifacts": [
            {
                "id": "weights",
                "selection_id": "primary",
                "file_id": "weights",
                "path": "model.safetensors",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
                "roles": ["entrypoint", "weights"],
                "mount": {
                    "source": "/run/vonk/models/primary",
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
    spec["identity"]["execution_sha256"] = execution_identity_sha256(spec)
    return spec


def _model_objects() -> list[dict[str, object]]:
    payload = b"verified model bytes"
    return [
        {
            "model_content_sha256": "e" * 64,
            "file_id": "weights",
            "path": "model.safetensors",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
            "roles": ["entrypoint", "weights"],
            "distribution_object": {
                "name": "model.safetensors",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
                "kind": "model",
            },
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
    assert artifact.mount.source == "/run/vonk/models/primary"
    assert artifact.materialized_path == "/run/vonk/models/primary/model.safetensors"
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
    objects[0]["path"] = "config.json"
    objects[0]["distribution_object"]["name"] = "config.json"
    with pytest.raises(CompiledExecutionPlanError, match="path, digest"):
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


def test_declared_execution_identity_must_cover_compiled_launch_facts() -> None:
    spec = _spec()
    spec["identity"]["execution_sha256"] = "0" * 64
    with pytest.raises(CompiledExecutionPlanError, match="launch facts"):
        compile_verified_execution_plan(
            spec,
            model_artifact_set_sha256="d" * 64,
            model_objects=_model_objects(),
            runtime_image=_image(),
        )


def test_plan_identity_does_not_mutate_canonical_runtime_input() -> None:
    spec = _spec()
    original = copy.deepcopy(spec)
    _compile(spec)
    assert spec == original


def _collision_spec() -> dict[str, object]:
    spec = _spec()
    spec["model_artifact_set_sha256"] = "9" * 64
    spec["model_dependencies"] = [
        {
            "selection_id": "primary",
            "publisher": "radixark",
            "slug": "qwen3-8-27b-nvfp4-009632fe",
            "content_sha256": "29b9d51b0a6dde0c2acae929c6d2a5651d19fb8a7572915f4c096e3b5bc5329b",
        },
        {
            "selection_id": "draft",
            "publisher": "radixark",
            "slug": "qwen3-8-27b-dspark-b3c99101",
            "content_sha256": "4091ffe98645f39f163c52efe1228f5385970df1d631df050eea1628b6721888",
        },
    ]
    qwen_sha = "66402a06352ac861bc9012a26678e6d5e11a5fd22180165fc19c8a27d3a9e079"
    dspark_sha = "dd65fb1b01c2adea69512ff2990a79d58eb7fe2c7ea97375aa66f657a29a5bfd"
    spec["artifacts"] = [
        {
            "id": "primary-config-66402a06352a",
            "selection_id": "primary",
            "file_id": "config-66402a06352a",
            "path": "config.json",
            "sha256": qwen_sha,
            "bytes": 72897,
            "roles": ["entrypoint"],
            "mount": {
                "source": "/run/vonk/models/primary",
                "target": "/models/target",
                "read_only": True,
            },
            "model": {
                "publisher": "radixark",
                "slug": "qwen3-8-27b-nvfp4-009632fe",
                "content_sha256": "29b9d51b0a6dde0c2acae929c6d2a5651d19fb8a7572915f4c096e3b5bc5329b",
            },
        },
        {
            "id": "dependency-qwen3-8-27b-dspark-b3c99101-config-dd65fb1b01c2",
            "selection_id": "draft",
            "file_id": "config-dd65fb1b01c2",
            "path": "config.json",
            "sha256": dspark_sha,
            "bytes": 2448,
            "roles": ["entrypoint"],
            "mount": {
                "source": "/run/vonk/models/draft",
                "target": "/models/draft",
                "read_only": True,
            },
            "model": {
                "publisher": "radixark",
                "slug": "qwen3-8-27b-dspark-b3c99101",
                "content_sha256": "4091ffe98645f39f163c52efe1228f5385970df1d631df050eea1628b6721888",
            },
        },
    ]
    spec["identity"]["execution_sha256"] = execution_identity_sha256(spec)
    return spec


def _collision_objects() -> list[dict[str, object]]:
    return [
        {
            "model_content_sha256": "29b9d51b0a6dde0c2acae929c6d2a5651d19fb8a7572915f4c096e3b5bc5329b",
            "file_id": "config-66402a06352a",
            "path": "config.json",
            "sha256": "66402a06352ac861bc9012a26678e6d5e11a5fd22180165fc19c8a27d3a9e079",
            "bytes": 72897,
            "roles": ["entrypoint"],
            "distribution_object": {
                "name": "config.json",
                "sha256": "66402a06352ac861bc9012a26678e6d5e11a5fd22180165fc19c8a27d3a9e079",
                "bytes": 72897,
                "kind": "model",
            },
        },
        {
            "model_content_sha256": "4091ffe98645f39f163c52efe1228f5385970df1d631df050eea1628b6721888",
            "file_id": "config-dd65fb1b01c2",
            "path": "config.json",
            "sha256": "dd65fb1b01c2adea69512ff2990a79d58eb7fe2c7ea97375aa66f657a29a5bfd",
            "bytes": 2448,
            "roles": ["entrypoint"],
            "distribution_object": {
                "name": "config.json",
                "sha256": "dd65fb1b01c2adea69512ff2990a79d58eb7fe2c7ea97375aa66f657a29a5bfd",
                "bytes": 2448,
                "kind": "model",
            },
        },
    ]


def test_qwen_config_collision_binds_model_identity_and_preserves_file_path(
    tmp_path,
) -> None:
    plan = compile_verified_execution_plan(
        _collision_spec(),
        model_artifact_set_sha256="9" * 64,
        model_objects=_collision_objects(),
        runtime_image=_image(),
    )
    models_root = tmp_path / "run" / "vonk" / "models"
    sizes = {"primary": 72897, "draft": 2448}
    prefixes = {"primary": b"qwen config", "draft": b"dspark config"}
    payloads = {
        selection: (prefix * ((size // len(prefix)) + 1))[:size]
        for selection, size in sizes.items()
        for prefix in [prefixes[selection]]
    }
    paths = {}
    for artifact in plan.artifacts:
        path = materialized_model_path(models_root, artifact)
        path.parent.mkdir(parents=True)
        path.write_bytes(payloads[artifact.selection_id])
        paths[artifact.selection_id] = path

    assert paths["primary"].name == "config.json"
    assert paths["draft"].name == "config.json"
    assert paths["primary"] != paths["draft"]
    assert paths["primary"].stat().st_size == 72897
    assert paths["draft"].stat().st_size == 2448
    assert paths["primary"].read_bytes().startswith(b"qwen config")
    assert paths["draft"].read_bytes().startswith(b"dspark config")
    assert all(
        item.sha256 not in str(path)
        for path in paths.values()
        for item in plan.artifacts
    )
    assert {item.mount.source for item in plan.artifacts} == {
        "/run/vonk/models/primary",
        "/run/vonk/models/draft",
    }


def test_qwen_collision_rejects_wrong_model_object_even_when_path_matches() -> None:
    with pytest.raises(CompiledExecutionPlanError, match="not covered"):
        compile_verified_execution_plan(
            _collision_spec(),
            model_artifact_set_sha256="9" * 64,
            model_objects=_collision_objects()[1:],
            runtime_image=_image(),
        )


def test_empty_model_support_file_requires_empty_digest_and_keeps_original_path(
    tmp_path,
) -> None:
    spec = _spec()
    spec["artifacts"][0].update(
        {
            "id": "tokenizer-config",
            "file_id": "tokenizer-config",
            "path": "tokenizer_config.json",
            "sha256": EMPTY_SHA256,
            "bytes": 0,
            "roles": ["auxiliary"],
        }
    )
    spec["identity"]["execution_sha256"] = execution_identity_sha256(spec)
    objects = [
        {
            "model_content_sha256": "e" * 64,
            "file_id": "tokenizer-config",
            "path": "tokenizer_config.json",
            "sha256": EMPTY_SHA256,
            "bytes": 0,
            "roles": ["auxiliary"],
            "distribution_object": {
                "name": "tokenizer_config.json",
                "sha256": EMPTY_SHA256,
                "bytes": 0,
                "kind": "model",
            },
        }
    ]
    plan = compile_verified_execution_plan(
        spec,
        model_artifact_set_sha256="d" * 64,
        model_objects=objects,
        runtime_image=_image(),
    )
    assert plan.model_artifact_set_bytes == 0
    artifact = plan.artifacts[0]
    path = materialized_model_path(tmp_path / "models", artifact)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")
    assert path.name == "tokenizer_config.json"
    assert path.read_bytes() == b""

    spec["artifacts"][0]["sha256"] = "a" * 64
    spec["identity"]["execution_sha256"] = execution_identity_sha256(spec)
    with pytest.raises(CompiledExecutionPlanError, match="digest or size"):
        compile_verified_execution_plan(
            spec,
            model_artifact_set_sha256="d" * 64,
            model_objects=objects,
            runtime_image=_image(),
        )
    with pytest.raises(ValidationError, match="only an empty model"):
        DistributionObjectReceipt.model_validate(
            {
                "name": "tokenizer_config.json",
                "sha256": "a" * 64,
                "bytes": 0,
                "kind": "model",
            }
        )
    invalid_roles = plan.artifacts[0].model_dump(mode="json")
    invalid_roles["roles"] = ["weights"]
    with pytest.raises(ValidationError, match="non-weight support"):
        CompiledModelArtifact.model_validate(invalid_roles)


def test_execution_identity_covers_compiled_launch_facts_and_ignores_notes() -> None:
    base = _spec()
    baseline = execution_identity_sha256(base)
    notes = copy.deepcopy(base)
    notes["editorial_notes"] = {"release": "same bytes"}
    notes["model_dependencies"][0]["artifact_key"] = "new-provenance-handle"
    assert execution_identity_sha256(notes) == baseline

    changes = []
    for key, value in (
        ("runtime", {"arguments": [{"name": "--max-model-len", "value": 4096}]}),
        ("security", {"user": "10002:10002"}),
        ("lifecycle", {"stop_timeout_seconds": 45}),
        ("topology", {"rank": 1}),
        ("endpoint", {"port": 8001}),
    ):
        changed = copy.deepcopy(base)
        changed[key].update(value)
        changes.append(execution_identity_sha256(changed))
    changed_artifact = copy.deepcopy(base)
    changed_artifact["artifacts"][0]["mount"]["target"] = "/models/changed"
    changes.append(execution_identity_sha256(changed_artifact))

    assert all(value != baseline for value in changes)
    assert len(set(changes)) == len(changes)
