import hashlib
from importlib.resources import files

from vonk_agent_protocol import canonical_message
from vonk_control.bounded_json import text
from vonk_control.resource_planning import (
    _resource_evidence,
    _selected_model_bytes,
    resolve_effective_settings,
)
from vonk_control.run_switch_operations import (
    _resource_evidence_digest,
    _settings_view,
)
from vonk_forge_contracts.model import ModelDefinition


def _model(publisher: str, slug: str, sizes: dict[str, int]) -> ModelDefinition:
    """Keep resource fixtures valid at the full catalog-document boundary."""
    template = ModelDefinition.model_validate_json(
        files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text(),
        strict=True,
    )
    candidate = template.model_copy(
        update={
            "identity": template.identity.model_copy(
                update={"publisher": publisher, "slug": slug}
            ),
            "files": [
                template.files[0].model_copy(
                    update={
                        "id": file_id,
                        "path": f"{file_id}.safetensors",
                        "size_bytes": size,
                        "sha256": hashlib.sha256(file_id.encode()).hexdigest(),
                    }
                )
                for file_id, size in sizes.items()
            ],
        }
    )
    return ModelDefinition.model_validate_json(
        canonical_message(candidate.model_dump(mode="json")), strict=True
    )


def _recipe() -> dict[str, object]:
    return {
        "models": [
            {
                "id": "primary",
                "model": {
                    "publisher": "radixark",
                    "slug": "qwen3-target",
                    "content_sha256": "a" * 64,
                },
                "files": [
                    {
                        "id": "weights",
                        "file_id": "weights",
                        "roles": ["worker"],
                        "mount": {"target": "/models/weights"},
                    },
                    {
                        "id": "tokenizer",
                        "file_id": "tokenizer",
                        "roles": ["worker"],
                        "mount": {"target": "/models/tokenizer"},
                    },
                ],
            }
        ],
        "settings": {
            "kind": "generation",
            "context_tokens": {"value": 32_768, "change_effect": "reprepare"},
            "concurrency": {"value": 1, "change_effect": "restart"},
            "knobs": {},
        },
        "topology": {
            "node_count": 1,
            "parallelism": {
                "tensor": 1,
                "pipeline": 1,
                "data": 1,
                "backend": "local",
            },
        },
    }


def test_selected_model_file_sizes_are_role_scoped_and_authoritative() -> None:
    model = _model(
        "radixark", "qwen3-target", {"weights": 900, "tokenizer": 100, "other": 4_000}
    )
    models = {("radixark", "qwen3-target", "a" * 64): model}
    assert _selected_model_bytes(_recipe(), models, "worker") == 1_000
    assert _selected_model_bytes(_recipe(), models, "other") is None


def test_published_qwen_dspark_corpus_scopes_target_and_drafter_bytes() -> None:
    recipe = {
        **_recipe(),
        "models": [
            {
                "id": "primary",
                "model": {
                    "publisher": "radixark",
                    "slug": "qwen3-8-27b-nvfp4-009632fe",
                    "content_sha256": "29b9d51b0a6dde0c2acae929c6d2a5651d19fb8a7572915f4c096e3b5bc5329b",
                },
                "files": [
                    {
                        "id": "model-00001-of-00003-fbcdb5ba1cdd",
                        "file_id": "model-00001-of-00003-fbcdb5ba1cdd",
                        "roles": ["entrypoint"],
                        "mount": {
                            "target": "/models/model-00001-of-00003-fbcdb5ba1cdd"
                        },
                    },
                    {
                        "id": "model-00002-of-00003-db6146a5464f",
                        "file_id": "model-00002-of-00003-db6146a5464f",
                        "roles": ["entrypoint"],
                        "mount": {
                            "target": "/models/model-00002-of-00003-db6146a5464f"
                        },
                    },
                    {
                        "id": "model-00003-of-00003-597573c145c2",
                        "file_id": "model-00003-of-00003-597573c145c2",
                        "roles": ["entrypoint"],
                        "mount": {
                            "target": "/models/model-00003-of-00003-597573c145c2"
                        },
                    },
                ],
            },
            {
                "id": "dependency-qwen3-8-27b-dspark-b3c99101",
                "model": {
                    "publisher": "radixark",
                    "slug": "qwen3-8-27b-dspark-b3c99101",
                    "content_sha256": "4091ffe98645f39f163c52efe1228f5385970df1d631df050eea1628b6721888",
                },
                "files": [
                    {
                        "id": "model-2aff025f4582",
                        "file_id": "model-2aff025f4582",
                        "roles": ["entrypoint"],
                        "mount": {"target": "/models/drafter"},
                    },
                ],
            },
        ],
    }
    models = {
        (
            "radixark",
            "qwen3-8-27b-nvfp4-009632fe",
            "29b9d51b0a6dde0c2acae929c6d2a5651d19fb8a7572915f4c096e3b5bc5329b",
        ): _model(
            "radixark",
            "qwen3-8-27b-nvfp4-009632fe",
            {
                "model-00001-of-00003-fbcdb5ba1cdd": 9_965_652_544,
                "model-00002-of-00003-db6146a5464f": 9_985_757_064,
                "model-00003-of-00003-597573c145c2": 3_797_923_080,
            },
        ),
        (
            "radixark",
            "qwen3-8-27b-dspark-b3c99101",
            "4091ffe98645f39f163c52efe1228f5385970df1d631df050eea1628b6721888",
        ): _model(
            "radixark",
            "qwen3-8-27b-dspark-b3c99101",
            {"model-2aff025f4582": 3_714_723_322},
        ),
    }
    assert _selected_model_bytes(recipe, models, "entrypoint") == 27_464_056_010
    assert _selected_model_bytes(recipe, models, "worker") is None


def test_run_switch_resource_view_binds_canonical_identity_and_evidence() -> None:
    recipe = _recipe()
    resolved = resolve_effective_settings(recipe)
    assert resolved.allowed and resolved.settings is not None
    view = _settings_view(resolved.settings)
    assert view.parallelism.world_size == 1
    assert view.identity_sha256 == resolved.settings.identity_digest

    evidence = _resource_evidence(
        recipe,
        "worker",
        {
            ("radixark", "qwen3-target", "a" * 64): _model(
                "radixark", "qwen3-target", {"weights": 900, "tokenizer": 100}
            )
        },
        1_200,
        resolved.settings,
    )
    assert evidence.weights_bytes == 1_000
    assert evidence.declared_total_bytes == 1_200
    assert evidence.evidence_state == "declared"
    assert _resource_evidence_digest("b" * 64) == "b" * 64
    assert _resource_evidence_digest(text(recipe.get("identity_sha256"))) is None
