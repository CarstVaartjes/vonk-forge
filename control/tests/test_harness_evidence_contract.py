from __future__ import annotations

import copy
import json

import pytest
from jsonschema import Draft202012Validator, ValidationError
from vonk_control.schema_resources import read_runtime_schema


def evidence_document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "recipe": {
            "publisher": "vonk-forge",
            "slug": "synthetic-tiny-openai",
            "content_sha256": "a" * 64,
        },
        "execution_harness": {
            "kind": "execution-harness",
            "publisher": "vonk-forge",
            "slug": "vllm-openai",
            "content_sha256": "b" * 64,
        },
        "runtime_distribution": {
            "kind": "runtime-distribution",
            "publisher": "vonk-forge",
            "slug": "python-312-cuda",
            "content_sha256": "c" * 64,
        },
        "patch_bundle": {
            "kind": "patch-bundle",
            "publisher": "vonk-forge",
            "slug": "cuda-fix",
            "content_sha256": "d" * 64,
        },
        "outcome": "passed",
        "artifacts": [{"name": "report.json", "sha256": "e" * 64}],
    }


def validator() -> Draft202012Validator:
    schema = json.loads(read_runtime_schema("harness-evidence-v1.schema.json"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def test_harness_evidence_accepts_exact_immutable_identities() -> None:
    validator().validate(evidence_document())


@pytest.mark.parametrize(
    ("field", "kind"),
    [
        ("recipe", "model-version"),
        ("execution_harness", "model-version"),
        ("runtime_distribution", "model-version"),
        ("patch_bundle", "model-version"),
    ],
)
def test_harness_evidence_rejects_wrong_purpose_kinds(
    field: str, kind: str
) -> None:
    document = copy.deepcopy(evidence_document())
    document[field]["kind"] = kind

    errors = list(validator().iter_errors(document))

    assert any(tuple(error.absolute_path)[:1] == (field,) for error in errors)
