"""Shared acceptance helper for recipe removal tests."""

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
) -> dict[str, object]:
    """Submit a removal of the selector against current state."""

    return service.remove_selector(
        selector,
        actor=actor,
        request_id=request_id,
        with_model=with_model,
    )
