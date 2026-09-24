from __future__ import annotations

import pytest
from pydantic import ValidationError
from vonk_agent_protocol import canonical_message
from vonk_control import cache_removal_review as review_contract
from vonk_control.cache_removal_review import (
    CacheRemovalAsset,
    CacheRemovalFinding,
    CacheRemovalReview,
    CacheRemovalReviewContent,
    seal_cache_removal_review,
)


def _content(
    *,
    observed_at: str = "2026-09-24T10:00:00Z",
    reverse: bool = False,
    with_model: bool | None = None,
) -> CacheRemovalReviewContent:
    first = CacheRemovalAsset(
        kind="model-set",
        sha256="a" * 64,
        expected_bytes=128,
        availability="verified",
        available_bytes=128,
        disposition="remove",
    )
    second = CacheRemovalAsset(
        kind="model-object",
        sha256="b" * 64,
        expected_bytes=256,
        availability="unknown",
        available_bytes=300,
        disposition="retain-shared",
    )
    finding = CacheRemovalFinding(
        classification="saved-reference",
        asset_kind="model-set",
        asset_sha256="a" * 64,
        owner_kind="fleet-profile",
        owner_id="profile-1",
        state="ready",
        detail="profile still selects this model set",
        reason="saved profile reference",
    )
    assets = [first, second]
    references = [finding]
    if reverse:
        assets.reverse()
        references.reverse()
    return CacheRemovalReviewContent(
        resource_kind="recipe" if with_model is not None else "model",
        selector="sample-model" if with_model is None else "recipe:sample",
        target_identity="c" * 64 if with_model is None else "d" * 40,
        with_model=with_model,
        assets=assets,
        references=references,
        active_work=[],
        blockers=[],
        observed_at=observed_at,
    )


def test_review_digest_binds_effects_but_not_observation_time_or_order() -> None:
    first = seal_cache_removal_review(_content())
    refreshed = seal_cache_removal_review(
        _content(observed_at="2026-09-24T10:01:00Z", reverse=True)
    )

    assert first.review_digest == refreshed.review_digest
    assert first.observed_at != refreshed.observed_at
    assert first.assets == refreshed.assets

    changed = _content().model_copy(update={"selector": "another-model"})
    assert seal_cache_removal_review(changed).review_digest != first.review_digest


def test_review_digest_binds_model_choice_and_observed_storage_bytes() -> None:
    without_model = seal_cache_removal_review(_content(with_model=False))
    with_model = seal_cache_removal_review(_content(with_model=True))
    assert without_model.review_digest != with_model.review_digest

    damaged_asset = CacheRemovalAsset(
        kind="model-object",
        sha256="b" * 64,
        expected_bytes=256,
        availability="unknown",
        available_bytes=400,
        disposition="retain-shared",
    )
    changed_storage = _content().model_copy(
        update={"assets": [_content().assets[0], damaged_asset]}
    )
    assert (
        seal_cache_removal_review(changed_storage).review_digest
        != seal_cache_removal_review(_content()).review_digest
    )


def test_review_validation_rejects_stale_digest_and_false_verified_bytes() -> None:
    sealed = seal_cache_removal_review(_content())
    document = sealed.model_dump(mode="json")
    document["selector"] = "other-model"
    with pytest.raises(ValidationError, match="digest does not match"):
        CacheRemovalReview.model_validate(document)

    with pytest.raises(ValidationError, match="exact expected byte length"):
        CacheRemovalAsset(
            kind="runtime-image",
            sha256="e" * 64,
            expected_bytes=50,
            availability="verified",
            available_bytes=49,
            disposition="remove",
        )


def test_complete_review_wire_budget_reports_limit_and_observed_size(
    monkeypatch,
) -> None:
    content = _content()
    review_document = content.model_dump(mode="json", exclude_none=False)
    review_document["review_digest"] = "0" * 64
    wire_size = len(canonical_message(review_document))
    content_size = len(canonical_message(content.model_dump(mode="json")))
    assert content_size < wire_size

    limit = wire_size - 1
    monkeypatch.setattr(review_contract, "MAX_CACHE_REMOVAL_REVIEW_BYTES", limit)
    with pytest.raises(
        ValidationError,
        match=rf"{limit}-byte limit \({wire_size} bytes observed\)",
    ):
        seal_cache_removal_review(content)
