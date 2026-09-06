from vonk_control.resource_planning import (
    resolve_effective_settings,
)
from vonk_control.run_switch_operations import (
    _resource_evidence,
    _resource_evidence_digest,
    _selected_model_bytes,
    _settings_view,
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
                    {"id": "weights", "file_id": "weights", "roles": ["worker"]},
                    {"id": "tokenizer", "file_id": "tokenizer", "roles": ["worker"]},
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
                "world_size": 1,
                "tensor": 1,
                "pipeline": 1,
                "data": 1,
                "backend": "local",
            },
        },
    }


def test_selected_model_file_sizes_are_role_scoped_and_authoritative() -> None:
    model = {
        "files": [
            {"id": "weights", "size_bytes": 900},
            {"id": "tokenizer", "size_bytes": 100},
            {"id": "other", "size_bytes": 4_000},
        ]
    }
    models = {("radixark", "qwen3-target", "a" * 64): model}
    assert _selected_model_bytes(_recipe(), models, "worker") == 1_000
    assert _selected_model_bytes(_recipe(), models, "other") is None


def test_published_qwen_dspark_corpus_scopes_target_and_drafter_bytes() -> None:
    recipe = {
        "models": [
            {
                "id": "primary",
                "model": {
                    "publisher": "radixark",
                    "slug": "qwen3-8-27b-nvfp4-009632fe",
                    "content_sha256": "29b9d51b0a6dde0c2acae929c6d2a5651d19fb8a7572915f4c096e3b5bc5329b",
                },
                "files": [
                    {"file_id": "model-00001-of-00003-fbcdb5ba1cdd", "roles": ["entrypoint"]},
                    {"file_id": "model-00002-of-00003-db6146a5464f", "roles": ["entrypoint"]},
                    {"file_id": "model-00003-of-00003-597573c145c2", "roles": ["entrypoint"]},
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
                    {"file_id": "model-2aff025f4582", "roles": ["entrypoint"]},
                ],
            },
        ]
    }
    models = {
        (
            "radixark",
            "qwen3-8-27b-nvfp4-009632fe",
            "29b9d51b0a6dde0c2acae929c6d2a5651d19fb8a7572915f4c096e3b5bc5329b",
        ): {
            "files": [
                {"id": "model-00001-of-00003-fbcdb5ba1cdd", "size_bytes": 9_965_652_544},
                {"id": "model-00002-of-00003-db6146a5464f", "size_bytes": 9_985_757_064},
                {"id": "model-00003-of-00003-597573c145c2", "size_bytes": 3_797_923_080},
            ]
        },
        (
            "radixark",
            "qwen3-8-27b-dspark-b3c99101",
            "4091ffe98645f39f163c52efe1228f5385970df1d631df050eea1628b6721888",
        ): {"files": [{"id": "model-2aff025f4582", "size_bytes": 3_714_723_322}]},
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
        {},
        "worker",
        {
            ("radixark", "qwen3-target", "a" * 64): {
                "files": [
                    {"id": "weights", "size_bytes": 900},
                    {"id": "tokenizer", "size_bytes": 100},
                ]
            }
        },
        1_200,
        resolved.settings,
    )
    assert evidence.weights_bytes == 1_000
    assert evidence.declared_total_bytes == 1_200
    assert evidence.evidence_state == "declared"
    assert _resource_evidence_digest("b" * 64) == "b" * 64
    assert _resource_evidence_digest(recipe.get("identity_sha256")) is None
