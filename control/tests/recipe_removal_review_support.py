"""Shared current-review acceptance helper for recipe removal tests."""

from vonk_control.recipe_image_availability import (
    RecipeImageAvailabilityService,
)


def remove_after_review(
    service: RecipeImageAvailabilityService,
    selector: str,
    *,
    actor: str,
    request_id: str,
    with_model: bool = False,
    review_digest: str | None = None,
) -> dict[str, object]:
    """Submit the digest from a current review unless replay tests supply it."""

    if review_digest is None:
        review_digest = service.review_removal(
            selector, with_model=with_model
        ).review_digest
    return service.remove_selector(
        selector,
        actor=actor,
        request_id=request_id,
        with_model=with_model,
        review_digest=review_digest,
    )
