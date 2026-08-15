from __future__ import annotations

import json
from pathlib import Path

import pytest
from vonk_control.catalog_contract import (
    CatalogContractError,
    CatalogKind,
    canonical_catalog_document,
    catalog_content_sha256,
    parse_catalog_json,
    parse_catalog_reference,
    validate_catalog_document,
)

ROOT = Path(__file__).resolve().parents[2]
MODEL_VERSION_FIXTURE = (
    ROOT / "control/tests/fixtures/catalog/model-version-v1-minimal.json"
)


def test_exact_reference_has_portable_immutable_identity() -> None:
    reference = parse_catalog_reference(
        {
            "kind": "model-version",
            "publisher": "vonk-forge",
            "slug": "synthetic-tiny-fp16",
            "content_sha256": "a" * 64,
        },
        expected_kind=CatalogKind.MODEL_VERSION,
    )

    assert reference.portable_identity == (
        "model-version",
        "vonk-forge",
        "synthetic-tiny-fp16",
        "a" * 64,
    )


def test_catalog_contract_rejects_territory_automation() -> None:
    document = json.loads(MODEL_VERSION_FIXTURE.read_text())
    document["license"]["excluded_territories"] = ["NL"]

    with pytest.raises(CatalogContractError):
        validate_catalog_document(document)


def test_catalog_parser_rejects_duplicate_keys_and_floats() -> None:
    with pytest.raises(CatalogContractError, match="duplicate object key"):
        parse_catalog_json(b'{"kind":"model","kind":"model"}')
    with pytest.raises(CatalogContractError, match="floats are not permitted"):
        parse_catalog_json(b'{"value":1.5}')


def test_catalog_canonicalization_is_stable() -> None:
    document = {"z": 1, "a": [True, None]}

    assert canonical_catalog_document(document) == b'{"a":[true,null],"z":1}'
    assert catalog_content_sha256(document) == (
        "ca6da02fba3343778761e7785f2b55f7fb17b36ce16eee3492dc392fa7c9deaa"
    )


def test_model_requires_a_model_group_reference() -> None:
    document = {
        "schema_version": 1,
        "kind": "model",
        "identity": {"publisher": "vonk-forge", "slug": "synthetic-tiny"},
        "metadata": {
            "title": "Synthetic Tiny",
            "description": "A model contract fixture.",
            "tags": ["synthetic"],
        },
        "model_group": {
            "kind": "model-group",
            "publisher": "vonk-forge",
            "slug": "synthetic",
            "content_sha256": "a" * 64,
        },
        "architecture": "synthetic",
    }

    validate_catalog_document(document)

    document["model_group"]["kind"] = "model"
    with pytest.raises(CatalogContractError):
        validate_catalog_document(document)


def test_model_version_requires_a_model_reference() -> None:
    document = json.loads(MODEL_VERSION_FIXTURE.read_text())

    validate_catalog_document(document)

    document["model"]["kind"] = "model-group"
    with pytest.raises(CatalogContractError):
        validate_catalog_document(document)


def test_harness_source_bundle_is_an_exact_build_input_not_a_catalog_reference() -> (
    None
):
    document = {
        "schema_version": 1,
        "kind": "execution-harness",
        "identity": {"publisher": "vonk-forge", "slug": "vllm-openai"},
        "metadata": {
            "title": "vLLM OpenAI",
            "description": "Harness contract fixture.",
            "tags": ["synthetic"],
        },
        "runtime_interface": "vonk.runtime.v1",
        "adapters": ["openai"],
        "source_bundle": {
            "sha256": "b" * 64,
            "expected_bytes": 2048,
            "media_type": "application/vnd.vonk-forge.source-bundle.v1+tar",
        },
    }

    validate_catalog_document(document)

    document["source_bundle"] = {
        "kind": "runtime-distribution",
        "publisher": "vonk-forge",
        "slug": "python-312-cuda",
        "content_sha256": "d" * 64,
    }
    with pytest.raises(CatalogContractError):
        validate_catalog_document(document)


def test_runtime_distribution_requires_one_exact_implemented_harness() -> None:
    document = {
        "schema_version": 1,
        "kind": "runtime-distribution",
        "identity": {"publisher": "vonk-forge", "slug": "python-312-cuda"},
        "metadata": {
            "title": "Python CUDA",
            "description": "Runtime distribution fixture.",
            "tags": ["synthetic"],
        },
        "implements_harness": {
            "kind": "execution-harness",
            "publisher": "vonk-forge",
            "slug": "vllm-openai",
            "content_sha256": "b" * 64,
        },
        "platform": "linux/arm64",
        "image": "registry.example/vonk/python@sha256:" + "c" * 64,
        "security": {
            "network_mode": "none",
            "user": "10001:10001",
            "no_new_privileges": True,
            "capabilities": [],
        },
        "sha256": "d" * 64,
    }

    validate_catalog_document(document)

    document.pop("implements_harness")
    with pytest.raises(CatalogContractError):
        validate_catalog_document(document)


def test_model_version_accepts_complete_strict_74_file_inventory() -> None:
    model = {
        "kind": "model",
        "publisher": "deepseek-ai",
        "slug": "deepseek-v4-flash-0731",
        "content_sha256": "a" * 64,
    }
    artifacts = [
        {
            "id": f"file-{index:02d}",
            "path": f"model-{index:05d}-of-00048.safetensors",
            "kind": "huggingface.file",
            "repository": "https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash-DSpark",
            "revision": "62af8fffb2f7030cac4de2f0169f5b8d1101b646",
            "sha256": f"{index + 1:064x}",
            "download_bytes": index + 1,
            "installed_bytes": index + 1,
            "roles": ["weights"],
        }
        for index in range(74)
    ]
    document = {
        "schema_version": 1,
        "kind": "model-version",
        "identity": {
            "publisher": "deepseek-ai",
            "slug": "deepseek-v4-flash-dspark",
        },
        "metadata": {
            "title": "DeepSeek V4 Flash DSpark",
            "description": "Exact public official checkpoint.",
            "tags": ["deepseek", "text"],
        },
        "model": model,
        "version": "0731-dspark-62af8ff",
        "source": {
            "repository": "https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash-DSpark",
            "revision": "62af8fffb2f7030cac4de2f0169f5b8d1101b646",
        },
        "lineage": {
            "publisher": "deepseek-ai",
            "relation": "official",
            "source_model": model,
            "derivation": "Official DSpark checkpoint.",
        },
        "format": {
            "container": "safetensors",
            "precision": "fp4-fp8-mixed",
            "quantization": "moe-experts-fp4-remaining-fp8",
        },
        "parameters": {"total": 284_000_000_000, "active": 13_000_000_000},
        "limits": {"context_tokens": 1_048_576},
        "sizes": {
            "download_bytes": sum(range(1, 75)),
            "installed_bytes": sum(range(1, 75)),
        },
        "license": {
            "spdx": "MIT",
            "url": "https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash-DSpark/blob/62af8fffb2f7030cac4de2f0169f5b8d1101b646/LICENSE",
            "attribution": ["DeepSeek AI"],
            "operator_acceptance_required": False,
        },
        "access": {
            "visibility": "public",
            "gated": False,
            "authentication": "none",
        },
        "artifacts": artifacts,
        "dependencies": [],
        "availability": "active",
        "supersedes": None,
    }

    validate_catalog_document(document)

    document["artifacts"][0]["unknown_fact"] = "hidden"
    with pytest.raises(CatalogContractError):
        validate_catalog_document(document)
