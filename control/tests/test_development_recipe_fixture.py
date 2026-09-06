from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

from vonk_control.recipe_runtime_specs import compile_runtime_spec
from vonk_control.source_policy import dockerfile_base_images

ROOT = Path(__file__).resolve().parents[2]


def _example(name: str) -> dict[str, object]:
    return json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", name)
        .read_text(encoding="utf-8")
    )


def test_synthetic_v2_source_build_compiles_with_a_canonical_receipt() -> None:
    recipe = _example("recipe-source-build.json")
    model = _example("model-definition.json")
    context = ROOT / "control/tests/fixtures/recipes/dev-http-smoke/context"
    base_images = dockerfile_base_images((context / "Dockerfile").read_bytes())
    expected_base_image = (
        "docker.io/library/python:3.12.11-slim-bookworm@"
        "sha256:9bb659dc6d5218917236f3711e866a5634bb4c2f208de9d4533aa4863f57c1d3"
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

    assert spec["runtime"]["entrypoint"] == [
        "/opt/vonk/bin/vllm",
        "serve",
        "/models",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ]
    assert spec["runtime"]["image"] == f"localhost/vonk/build@sha256:{digest}"
    assert spec["security"]["mounts"] == [
        {
            "source": "/run/vonk/models/primary",
            "target": "/models",
            "read_only": True,
        },
        {"source": "/run/vonk/outputs", "target": "/outputs", "read_only": False},
    ]
