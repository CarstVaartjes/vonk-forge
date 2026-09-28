from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from vonk_control.recipe_runtime_specs import compile_runtime_spec
from vonk_control.source_policy import dockerfile_base_images
from vonk_forge_contracts import document_sha256

from .canonical_recipe_fixtures import canonical_example

ROOT = Path(__file__).resolve().parents[2]


def _example(name: str) -> dict[str, object]:
    return canonical_example(name)


def test_synthetic_v2_source_build_compiles_with_a_canonical_receipt() -> None:
    recipe = _example("recipe-source-build.json")
    model = _example("model-definition.json")
    context = ROOT / "control/tests/fixtures/recipes/dev-http-smoke/context"
    base_images = dockerfile_base_images((context / "Dockerfile").read_bytes())
    expected_base_image = (
        "docker.io/library/python:3.14.7-slim-trixie@"
        "sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d"
    )
    assert tuple(image["reference"] for image in base_images) == (expected_base_image,)

    digest = "d" * 64
    spec = compile_runtime_spec(
        recipe,
        models={document_sha256(model): model},
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
        {"source": "/run/vonk/models/primary", "target": "/models"},
        {"source": "/run/vonk/outputs", "target": "/outputs"},
    ]
