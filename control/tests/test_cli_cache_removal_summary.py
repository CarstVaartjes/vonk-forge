"""The removal review answers the consent question in a few lines."""

from __future__ import annotations

import hashlib
import json

import pytest
from vonk_agent_protocol import (
    ArtifactLifecycleCode,
    AssetAvailability,
    ModelCacheCode,
)
from vonk_control.cache_removal_review import (
    CacheAssetDisposition,
    CacheRemovalAsset,
    CacheRemovalBlocker,
    CacheRemovalFinding,
    CacheRemovalReviewContent,
    seal_cache_removal_review,
)

from cluster_profiles import cli
from cluster_profiles.cli_render import render_payload

from .test_cli_cache_removal_review import (
    _accept_response_contracts,  # noqa: F401
    _fake_transport_reviews_are_contract_validated,  # noqa: F401
    _FakeController,
)

_SELECTOR = "publisher/model"
_GIB = 1 << 30


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _asset(
    kind: str, label: str, disposition: CacheAssetDisposition, size: int = _GIB
) -> CacheRemovalAsset:
    return CacheRemovalAsset(
        kind=kind,  # type: ignore[arg-type]
        sha256=_digest(label),
        expected_bytes=size,
        availability=AssetAvailability.VERIFIED,
        available_bytes=size,
        disposition=disposition,
    )


def _sharing(label: str, other_set: str) -> CacheRemovalFinding:
    return CacheRemovalFinding(
        classification="saved-reference",
        asset_kind="model-object",
        asset_sha256=_digest(label),
        owner_kind="model-cache-set-membership",
        owner_id=_digest(other_set),
        state="complete",
        detail="another cached model set shares this object; removal will retain it",
        reason=f"shared object is retained by model set {_digest(other_set)}",
    )


def _review(*, blocked: bool = False) -> dict[str, object]:
    """41 removed + 9 shared objects in 2 selected sets; 13 sets share the 9."""
    removed = [
        _asset("model-object", f"gone-{i}", CacheAssetDisposition.REMOVE)
        for i in range(41)
    ]
    kept = [
        _asset("model-object", f"shared-{i}", CacheAssetDisposition.RETAIN_SHARED)
        for i in range(9)
    ]
    sets = [
        _asset("model-set", f"set-{i}", CacheAssetDisposition.REMOVE, 25 * _GIB)
        for i in range(2)
    ]
    references = [
        _sharing(f"shared-{i}", f"other-{j}") for i in range(9) for j in range(13)
    ]
    active: list[CacheRemovalFinding] = []
    blockers: list[CacheRemovalBlocker] = []
    if blocked:
        profile = CacheRemovalFinding(
            classification="saved-reference",
            asset_kind="model-set",
            asset_sha256=_digest("set-0"),
            owner_kind="fleet-profile",
            owner_id="profile-4",
            state="current",
            detail="Saved profile selects this model.",
            reason="profile-4 still selects this model",
        )
        work = CacheRemovalFinding(
            classification="active-work",
            asset_kind="model-set",
            asset_sha256=_digest("set-1"),
            owner_kind="model-cache-operation",
            owner_id="operation-7",
            state="running",
            detail="accepted model removal retains this deletion fence",
            reason="removal operation operation-7 is running and owns model-set x",
        )
        references.append(profile)
        active.append(work)
        blockers = [
            CacheRemovalBlocker(
                code=ModelCacheCode.REMOVAL_REFERENCED,
                detail=profile.reason,
                retryable=False,
                recovery_actions=["resolve_reference"],
            ),
            CacheRemovalBlocker(
                code=ArtifactLifecycleCode.DELETION_IN_PROGRESS,
                detail=work.reason,
                retryable=True,
                recovery_actions=["observe_removal_operation"],
            ),
        ]
    content = CacheRemovalReviewContent(
        resource_kind="model",
        selector=_SELECTOR,
        target_identity=_digest("model"),
        with_model=None,
        assets=[*sets, *removed, *kept],
        references=references,
        active_work=active,
        blockers=blockers,
        observed_at="2026-10-30T10:00:00Z",
    )
    return seal_cache_removal_review(content).model_dump(mode="json")


def _human(capsys: pytest.CaptureFixture[str], review: dict, **kwargs) -> str:
    render_payload(review, "model", action="preview", **kwargs)
    return capsys.readouterr().out


def test_default_review_is_a_summary(capsys: pytest.CaptureFixture[str]) -> None:
    review = _review()
    text = _human(capsys, review)

    assert "Removes 41.0 GiB (44023414784 bytes) in 41 files (2 model sets)" in text
    assert (
        "Keeps 9 shared files (9.0 GiB (9663676416 bytes)) also used by 13 other cached model sets"
        in text
    )
    assert "Blockers: none" in text
    assert "Active work: none" in text
    assert f"Confirm: vonkctl model remove {_SELECTOR} --yes --detach" in text
    assert "Review digest: " + str(review["review_digest"]) in text
    # Short by content: no per-asset or per-pair blocks, whatever the model size.
    assert "Asset:" not in text and "Saved references:\n" not in text
    assert "Saved references: none" in text
    assert text.count("another cached model set") == 0
    for asset in review["assets"]:  # type: ignore[attr-defined]
        assert asset["sha256"] not in text


def test_verbose_lists_each_shared_object_once(
    capsys: pytest.CaptureFixture[str],
) -> None:
    review = _review()
    text = _human(capsys, review, wide=True)

    for i in range(9):
        assert text.count(_digest(f"shared-{i}")) == 2  # asset + one sharing line
        line = next(
            row
            for row in text.splitlines()
            if row.startswith("Shared ") and _digest(f"shared-{i}") in row
        )
        assert all(_digest(f"other-{j}") in line for j in range(13))
    assert text.count("Asset:") == 52
    assert "Saved references:\n" not in text


def test_blocked_review_prints_only_the_controllers_blockers(
    capsys: pytest.CaptureFixture[str],
) -> None:
    review = _review(blocked=True)
    text = _human(capsys, review)

    assert "Saved references: fleet-profile (current): fleet-profile profile-4" in text
    assert (
        "Active work: model-cache-operation (running): "
        "model-cache-operation operation-7" in text
    )
    assert (
        f"Blocked: {ModelCacheCode.REMOVAL_REFERENCED} (not retryable): "
        "profile-4 still selects this model" in text
    )
    assert f"Blocked: {ArtifactLifecycleCode.DELETION_IN_PROGRESS} (retryable)" in text
    assert "  Next: resolve_reference" in text
    assert "  Next: observe_removal_operation" in text
    # The Controller decides on submit; the confirm command is always shown.
    assert f"Confirm: vonkctl model remove {_SELECTOR} --yes --detach" in text


def test_recipe_aggregated_blocker_is_shown_as_the_controller_sent_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    review = _review()
    review["resource_kind"] = "recipe"
    review["with_model"] = False
    review["target_identity"] = "revision-1"
    review["blockers"] = [
        CacheRemovalBlocker(
            code=ModelCacheCode.REMOVAL_REFERENCED,
            detail="runtime image references prevent removal (2 owner(s)): a; b",
            retryable=True,
            recovery_actions=["inspect"],
        ).model_dump(mode="json")
    ]
    text = _human(capsys, review)

    assert (
        f"Blocked: {ModelCacheCode.REMOVAL_REFERENCED} (retryable): "
        "runtime image references prevent removal (2 owner(s)): a; b" in text
    )
    assert "  Next: inspect" in text
    assert f"Confirm: vonkctl recipe remove {_SELECTOR} --keep-model --yes" in text


def test_verbose_lists_every_blocker_in_full(
    capsys: pytest.CaptureFixture[str],
) -> None:
    review = _review()
    review["blockers"] = [
        CacheRemovalBlocker(
            code=ModelCacheCode.REMOVAL_REFERENCED,
            detail=f"owner {i} prevents removal",
            retryable=True,
        ).model_dump(mode="json")
        for i in range(3)
    ]
    short = _human(capsys, review)
    full = _human(capsys, review, wide=True)

    assert "x3" in short
    assert all(f"owner {i} prevents removal" in full for i in range(3))


def test_json_review_is_unchanged_by_verbose(
    capsys: pytest.CaptureFixture[str],
) -> None:
    review = _review()
    outputs = []
    for extra in ((), ("--verbose",)):
        status = cli.main(
            ("--json", "model", "remove", _SELECTOR, "--review", *extra),
            control_client=_FakeController(review),
        )
        assert status == 0
        outputs.append(capsys.readouterr().out)

    assert outputs[0] == outputs[1]
    assert json.loads(outputs[0]) == review


def test_fleet_review_confirm_command_keeps_the_fleet_scope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from vonk_control.fleet_model_removal_contract import (
        FleetModelRemovalEffects,
        FleetRemovalReason,
    )

    review = _review()
    review["fleet"] = FleetModelRemovalEffects(
        nas_remove=True,
        nas_reason=FleetRemovalReason.UNUSED,
        nas_detail="Unused model",
        targets=[],
    ).model_dump(mode="json")
    text = _human(capsys, review)

    assert "NAS removal: yes" in text
    assert f"Confirm: vonkctl model remove {_SELECTOR} --fleet --yes --detach" in text


def test_protected_nas_copy_without_blockers_is_not_blocked(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from vonk_control.fleet_model_removal_contract import (
        FleetModelRemovalEffects,
        FleetRemovalReason,
    )

    review = _review()
    review["fleet"] = FleetModelRemovalEffects(
        nas_remove=False,
        nas_reason=FleetRemovalReason.REFERENCED,
        nas_detail="A saved profile keeps the NAS copy",
        targets=[],
    ).model_dump(mode="json")
    text = _human(capsys, review)

    assert "NAS removal: no" in text
    assert "Blocked" not in text
    assert f"Confirm: vonkctl model remove {_SELECTOR} --fleet --yes --detach" in text
