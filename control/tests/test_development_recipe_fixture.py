from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from vonk_control.recipe_runtime_specs import compile_runtime_spec
from vonk_control.source_policy import dockerfile_base_images
from vonk_forge_contracts import ModelDefinition

from .canonical_recipe_fixtures import canonical_example

ROOT = Path(__file__).resolve().parents[2]


def _example(name: str) -> dict[str, object]:
    return canonical_example(name)


def test_synthetic_v2_source_build_compiles_with_a_canonical_receipt() -> None:
    recipe = _example("recipe-source-build.json")
    model = ModelDefinition.model_validate(_example("model-definition.json"))
    context = ROOT / "control/tests/fixtures/recipes/dev-http-smoke/context"
    base_images = dockerfile_base_images((context / "Dockerfile").read_bytes())
    expected_base_image = (
        "docker.io/library/python:3.14.7-slim-bookworm@"
        "sha256:9ab8d9c8514b44f90cf0029dd42fdd7e9e211e639c8b995304cc04568dee900f"
    )
    assert tuple(image["reference"] for image in base_images) == (expected_base_image,)

    digest = "d" * 64
    spec = compile_runtime_spec(
        recipe,
        models=[model],
        package_handle={
            "image_digest": digest,
            "image_reference": f"localhost/vonk/build@sha256:{digest}",
            "paths": ["context.tar", "Dockerfile"],
        },
        role="entrypoint",
        rank=0,
    )

    runtime = spec["runtime"]
    assert isinstance(runtime, Mapping)
    assert runtime["entrypoint"] == [
        "/opt/vonk/bin/vllm",
        "serve",
        "/models",
        "--unknown-option",
        "value with spaces; $HOME/Δ and {json}",
        "--structured_option",
        '{"enabled":true,"items":["a",3,0.25]}',
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ]
    assert runtime["image"] == f"localhost/vonk/build@sha256:{digest}"
    security = spec["security"]
    assert isinstance(security, Mapping)
    assert security["mounts"] == [
        {
            "source": "/run/vonk/models/primary",
            "target": "/models",
            "read_only": True,
        },
        {"source": "/run/vonk/outputs", "target": "/outputs", "read_only": False},
    ]
