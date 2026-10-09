from __future__ import annotations

import pytest
from pydantic import ValidationError
from vonk_control.run_switch_contract import (
    RunSwitchReconciliationAuthority,
    RunSwitchReconciliationTarget,
)


def _target(node_suffix: str, rank: int) -> RunSwitchReconciliationTarget:
    return RunSwitchReconciliationTarget(
        node_id=f"spk_{node_suffix * 32}",
        rank=rank,
        role="worker",
        installed_bytes=128,
        state="pending",
    )


def _authority(
    targets: list[RunSwitchReconciliationTarget], **overrides: object
) -> RunSwitchReconciliationAuthority:
    values: dict[str, object] = {
        "installation_id": "00000000-0000-4000-8000-000000000001",
        "original_plan_digest": "c" * 64,
        "recipe_revision_id": "00000000-0000-4000-8000-000000000002",
        "recipe_content_sha256": "d" * 64,
        "mapping_id": "00000000-0000-4000-8000-000000000003",
        "mapping_generation": 1,
        "recipe_build_id": None,
        "image_digest": f"sha256:{'e' * 64}",
        "model_content_sha256": None,
        "targets": targets,
    }
    values.update(overrides)
    return RunSwitchReconciliationAuthority.model_validate(values)


def test_reconciliation_authority_uses_current_plan_schema_and_contiguous_targets() -> (
    None
):
    authority = _authority([_target("1", 0), _target("2", 1)])

    assert (
        RunSwitchReconciliationAuthority.model_validate_json(
            authority.model_dump_json()
        )
        == authority
    )
    assert [target.rank for target in authority.targets] == [0, 1]


@pytest.mark.parametrize(
    "targets",
    [
        [_target("1", 0), _target("1", 1)],
        [_target("1", 0), _target("2", 0)],
        [_target("1", 0), _target("2", 2)],
        [_target("2", 1), _target("1", 0)],
    ],
)
def test_reconciliation_authority_rejects_ambiguous_target_membership(
    targets: list[RunSwitchReconciliationTarget],
) -> None:
    with pytest.raises(ValidationError):
        _authority(targets)
    fresh = _authority([_target("1", 0), _target("2", 1)])
    assert (
        RunSwitchReconciliationAuthority.model_validate_json(fresh.model_dump_json())
        == fresh
    )


def test_reconciliation_authority_rejects_a_legacy_nested_schema_version() -> None:
    with pytest.raises(ValidationError):
        _authority([_target("1", 0)], schema_version=1)
