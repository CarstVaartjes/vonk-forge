from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "control/src"))

from vonk_control.catalog_contract import (
    catalog_content_sha256,
    validate_catalog_document,
)
from vonk_control.recipe_contract import validate_recipe
from vonk_control.recipe_runtime_specs import compile_runtime_spec
from vonk_control.source_bundles import generate_source_bundle
from vonk_control.source_policy import enforce_build_source_policy

KIND_ROOT = {
    "model-group": "model-groups",
    "model": "models",
    "model-version": "model-versions",
    "execution-harness": "execution-harnesses",
    "runtime-distribution": "runtime-distributions",
    "patch-bundle": "patch-bundles",
}
GGUF_REVISION = "e7f04037032990db0346398d249baf9fb9df1ccc"


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _recipe(slug: str) -> dict[str, object]:
    return _load(ROOT / "config/recipes" / f"{slug}.json")


def _resolve(reference: dict[str, object]) -> dict[str, object]:
    document = _load(
        ROOT
        / "config"
        / KIND_ROOT[str(reference["kind"])]
        / f"{reference['slug']}.json"
    )
    validate_catalog_document(document)
    assert document["identity"]["publisher"] == reference["publisher"]
    assert document["identity"]["slug"] == reference["slug"]
    assert catalog_content_sha256(document) == reference["content_sha256"]
    return document


def _bundle(context: Path):
    return generate_source_bundle(
        {
            path.relative_to(context).as_posix(): path.read_bytes()
            for path in sorted(context.rglob("*"))
            if path.is_file()
        }
    )


def test_ds4_recipe_is_one_node_and_uses_ds4_harness() -> None:
    recipe = _recipe("deepseek-v4-flash-0731-ds4-single")
    validate_recipe(recipe)

    harness = _resolve(recipe["execution"]["harness"])
    version = _resolve(recipe["model"])
    distribution = _resolve(recipe["runtime"]["distribution"])

    assert recipe["topology"]["mode"] == "single"
    assert recipe["topology"]["node_count"] == 1
    assert recipe["topology"]["parallelism"]["world_size"] == 1
    assert harness["compiler_slug"] == "ds4"
    assert version["format"]["container"] == "gguf"
    assert version["format"]["precision"] == "mixed"
    assert version["format"]["quantization"] == "iq2_xxs-q2_k-mixed"
    assert distribution["capabilities"]["distributed_vllm"] is None


def test_ds4_model_version_has_exact_current_target_and_dspark_support() -> None:
    version = _resolve(_recipe("deepseek-v4-flash-0731-ds4-single")["model"])
    artifacts = {artifact["id"]: artifact for artifact in version["artifacts"]}

    assert version["source"] == {
        "repository": "https://huggingface.co/antirez/deepseek-v4-gguf",
        "revision": GGUF_REVISION,
    }
    assert version["lineage"]["derivation"] == (
        "imatrix mixed quantization with routed-expert gate/up tensors at "
        "IQ2_XXS, routed-expert down tensors at Q2_K, attention projections "
        "at Q8_0, shared experts at Q8_0, and output at Q8_0"
    )
    assert artifacts["target"] == {
        "id": "target",
        "path": "DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix-0731.gguf",
        "kind": "http.file",
        "repository": "https://huggingface.co/antirez/deepseek-v4-gguf",
        "revision": GGUF_REVISION,
        "sha256": "ca22ae2f838e14077c22bc1c1417b71b45b5e5a3687bd96c2ac6e17fdb6261c0",
        "download_bytes": 86_720_111_488,
        "installed_bytes": 86_720_111_488,
        "roles": ["target"],
    }
    assert artifacts["drafter"] == {
        "id": "drafter",
        "path": "DeepSeek-V4-Flash-DSpark-support-0731.gguf",
        "kind": "http.file",
        "repository": "https://huggingface.co/antirez/deepseek-v4-gguf",
        "revision": GGUF_REVISION,
        "sha256": "7e319924541db3f7a163ed7e11d7532a70d48228ab59d36cb81e1d4511885360",
        "download_bytes": 5_989_114_272,
        "installed_bytes": 5_989_114_272,
        "roles": ["drafter"],
    }
    assert version["sizes"] == {
        "download_bytes": 92_709_225_760,
        "installed_bytes": 92_709_225_760,
    }
    assert version["access"] == {
        "visibility": "public",
        "gated": False,
        "authentication": "none",
    }


def test_ds4_distribution_and_build_are_immutable_and_runtime_is_offline() -> None:
    recipe = _recipe("deepseek-v4-flash-0731-ds4-single")
    distribution = _resolve(recipe["runtime"]["distribution"])
    context = ROOT / "adapters/deepseek/ds4"
    bundle = _bundle(context)

    assert distribution["source"] == {
        "repository": "https://github.com/antirez/ds4",
        "revision": "84cc882352757baf628a1776badf7cc54d584e28",
        "archive_sha256": "3ab2c4485bee87f36166b12ab59abbc293ad9fdfadb1c2920d1cbc7f617da165",
        "license": "MIT",
    }
    assert "@sha256:" in distribution["image"]
    assert recipe["runtime"]["lifecycle"]["pre_start"] == []
    assert recipe["runtime"]["environment"] == [
        {"name": "DS4_LOG_LEVEL", "value": "INFO"},
        {"name": "HF_HUB_OFFLINE", "value": "1"},
    ]
    assert recipe["build"]["context"] == {
        "path": "adapters/deepseek/ds4",
        "sha256": bundle.sha256,
        "expected_bytes": len(bundle.archive),
        "media_type": "application/vnd.vonk-forge.source-bundle.v1+tar",
    }
    assert enforce_build_source_policy(recipe, bundle).passed is True
    dockerfile = (context / "Dockerfile").read_text(encoding="utf-8")
    assert "84cc882352757baf628a1776badf7cc54d584e28" in dockerfile
    assert "ARG DS4_SOURCE_" not in dockerfile
    assert "ca22ae2f838e" not in dockerfile
    assert dockerfile.rstrip().endswith("USER 10001:10001\nENTRYPOINT []")


def test_ds4_validation_is_bounded_openai_validation() -> None:
    recipe = _recipe("deepseek-v4-flash-0731-ds4-single")

    assert recipe["interfaces"] == [
        {
            "adapter": "openai",
            "port": 8080,
            "model_aliases": ["deepseek-v4-flash"],
            "health_path": "/v1/models",
        }
    ]
    assert recipe["validation"]["validators"] == [
        {
            "interface": "openai",
            "checks": [
                "endpoint.healthy",
                "chat.nonempty",
                "chat.max-output-64",
            ],
        }
    ]


def test_ds4_runtime_spec_preserves_exact_declared_mount_authority() -> None:
    recipe = _recipe("deepseek-v4-flash-0731-ds4-single")
    harness = _resolve(recipe["execution"]["harness"])
    distribution = _resolve(recipe["runtime"]["distribution"])

    spec = compile_runtime_spec(
        recipe,
        resolved_entities={
            "model_version": SimpleNamespace(
                content_sha256=recipe["model"]["content_sha256"]
            ),
            "harness": SimpleNamespace(
                document=harness,
                content_sha256=recipe["execution"]["harness"]["content_sha256"],
            ),
            "runtime_distribution": SimpleNamespace(
                document=distribution,
                content_sha256=recipe["runtime"]["distribution"]["content_sha256"],
            ),
            "patch_bundle": None,
        },
        parameters={},
        role="entrypoint",
        rank=0,
        recipe_build_id="00000000-0000-4000-8000-000000000001",
        image_digest="sha256:" + "d" * 64,
    )

    assert spec["security"]["mounts"] == [
        {"source": "model", "target": "/models", "read_only": True},
        {"source": "outputs", "target": "/outputs", "read_only": False},
    ]
