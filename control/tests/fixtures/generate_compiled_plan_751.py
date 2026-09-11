"""Generate the checked-in 751-artifact launch fixture through production code."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from vonk_control.compiled_execution_plan import (
    compile_verified_execution_plan,
    execution_identity_sha256,
)
from vonk_control.execution_plan_service import _bind_runtime_artifacts

from control.tests.test_compiled_execution_plan import _image, _spec


def _json_object(value: object) -> dict[str, object]:
    """Narrow one decoded JSON object so the fixture build stays typed."""

    assert isinstance(value, dict)
    return value


def _json_array(value: object) -> list[object]:
    """Narrow one decoded JSON array so the fixture build stays typed."""

    assert isinstance(value, list)
    return value


def main() -> None:
    runtime_spec = copy.deepcopy(_spec())
    role_names = [
        "entrypoint",
        "weights",
        "fixture-role-" + "x" * 51,
        "fixture-padding-" + "x" * 11,
    ]
    artifacts = []
    model_files = []
    model_objects = []
    model_digest = "e" * 64
    for index in range(751):
        file_id = f"artifact-{index:04d}"
        path = f"model/artifact-{index:04d}.safetensors"
        content = f"fixture-artifact-{index}".encode()
        digest = hashlib.sha256(content).hexdigest()
        artifacts.append(
            {
                "id": file_id,
                "selection_id": "primary",
                "file_id": file_id,
                "path": path,
                "sha256": digest,
                "bytes": len(content),
                "roles": role_names,
                "mount": {
                    "source": "/run/vonk/models/primary",
                    "target": "/models",
                    "read_only": True,
                },
                "model": {
                    "publisher": "vonk-forge",
                    "slug": "compiled-plan-fixture",
                    "content_sha256": model_digest,
                },
            }
        )
        model_objects.append(
            {
                "model_content_sha256": model_digest,
                "file_id": file_id,
                "path": path,
                "sha256": digest,
                "bytes": len(content),
                "roles": role_names,
                "distribution_object": {
                    "name": path,
                    "sha256": digest,
                    "bytes": len(content),
                    "kind": "model",
                },
            }
        )
        model_files.append(
            {
                "id": file_id,
                "path": path,
                "sha256": digest,
                "size_bytes": len(content),
                "roles": role_names,
            }
        )
    runtime_spec["artifacts"] = artifacts
    _json_object(_json_array(runtime_spec["model_dependencies"])[0])["content_sha256"] = model_digest
    _json_object(runtime_spec["identity"])["execution_sha256"] = execution_identity_sha256(runtime_spec)
    bound = _bind_runtime_artifacts(
        runtime_spec,
        [
            type(
                "Revision",
                (),
                {"document": {"files": model_files}, "content_digest": model_digest},
            )()
        ],
    )
    plan = compile_verified_execution_plan(
        bound,
        model_artifact_set_sha256="d" * 64,
        model_objects=model_objects,
        runtime_image=_image(),
    )
    payload = plan.to_compiled_launch_payload(
        bound,
        placement={
            "endpoint_address": None,
            "rank": 0,
            "role": "entrypoint",
            "world_size": 1,
            "local_address": None,
            "master_address": None,
            "master_port": None,
            "port": 8000,
            "reserved_memory_bytes": 1,
        },
    )
    output = Path(__file__).with_name("compiled_plan_751.json")
    output.write_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    print(output, output.stat().st_size, len(_json_array(payload["artifacts"])))


if __name__ == "__main__":
    main()
