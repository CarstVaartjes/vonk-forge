import pytest
from pydantic import TypeAdapter
from vonk_forge_contracts.model import Slug
from vonk_forge_contracts.recipe import RecipeIdentity

from vonk_control.auth import CursorCodec, CursorError
from vonk_control.cursor_contract import MAX_CURSOR_LENGTH


def test_cursor_signer_rejects_short_signing_key() -> None:
    with pytest.raises(ValueError):
        CursorCodec.from_signing_key(b"short")


def test_cursor_fits_longest_canonical_selector_and_remains_query_bound() -> None:
    slug_adapter = TypeAdapter(Slug)
    maximum_slug = "a" * slug_adapter.json_schema()["maxLength"]
    assert slug_adapter.validate_python(maximum_slug) == maximum_slug
    model_selector = f"{maximum_slug}/{maximum_slug}"
    recipe_identity = RecipeIdentity(
        publisher=maximum_slug,
        slug=maximum_slug,
    )
    recipe_selector = f"{recipe_identity.publisher}/{recipe_identity.slug}"
    context = {"l": 512, "s": "updated", "f": "f" * 64}
    codec = CursorCodec.from_signing_key(b"c" * 32)

    for resource, selector in (
        ("models", model_selector),
        ("recipes", recipe_selector),
    ):
        boundary = ["99991231T235959Z", selector, "a" * 64]
        cursor = codec.encode(
            resource=resource,
            order="catalog",
            context=context,
            boundary=boundary,
        )
        assert len(cursor) <= MAX_CURSOR_LENGTH
        assert (
            codec.decode(
                cursor,
                resource=resource,
                order="catalog",
                context=context,
            )
            == boundary
        )
        with pytest.raises(CursorError):
            codec.decode(
                cursor,
                resource=resource,
                order="catalog",
                context={**context, "f": "e" * 64},
            )
