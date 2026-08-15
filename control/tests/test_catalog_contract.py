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

    with pytest.raises(CatalogContractError, match="additionalProperties"):
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
