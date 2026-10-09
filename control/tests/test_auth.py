import pytest
from pydantic import TypeAdapter
from vonk_control.auth import (
    Actor,
    CursorError,
    TokenCodec,
)
from vonk_control.cursor_contract import MAX_CURSOR_LENGTH
from vonk_forge_contracts.model import Slug
from vonk_forge_contracts.recipe import RecipeIdentity


def test_signed_token_round_trip_and_tamper_rejection() -> None:
    codec = TokenCodec(b"x" * 32)
    token = codec.issue(Actor("admin", "administrator"), ttl_seconds=60, now=100)
    assert codec.verify(token, now=120) == Actor("admin", "administrator")
    accepted = []
    with pytest.raises(Exception):  # noqa: B017 -- tampered token yields no identity
        accepted.append(codec.verify(token + "changed", now=120))
    assert not accepted
    with pytest.raises(Exception):  # noqa: B017 -- expired token yields no identity
        accepted.append(codec.verify(token, now=161))
    assert not accepted
    fresh = codec.issue(Actor("admin", "administrator"), ttl_seconds=60, now=161)
    assert codec.verify(fresh, now=162) == Actor("admin", "administrator")


def test_codec_rejects_short_signing_key() -> None:
    with pytest.raises(ValueError):
        TokenCodec(b"short")


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
    codec = TokenCodec(b"c" * 32).cursor_codec()

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
