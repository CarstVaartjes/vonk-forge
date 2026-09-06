from vonk_control.resource_planning import (
    resolve_effective_settings,
)
from vonk_control.run_switch_operations import (
    _resource_evidence,
    _selected_model_bytes,
    _settings_view,
)


def _recipe() -> dict[str, object]:
    return {
        "models": [
            {
                "id": "primary",
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
    assert _selected_model_bytes(_recipe(), model, "worker") == 1_000
    assert _selected_model_bytes(_recipe(), model, "other") is None


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
        {"files": [{"id": "weights", "size_bytes": 900}, {"id": "tokenizer", "size_bytes": 100}]},
        1_200,
        resolved.settings,
    )
    assert evidence.weights_bytes == 1_000
    assert evidence.declared_total_bytes == 1_200
    assert evidence.evidence_state == "declared"
