from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import Literal, cast






T = TypeVar("T", bound="RecipeCacheRemovalIntent")



@_attrs_define
class RecipeCacheRemovalIntent:
    """ Exact accepted removal request stored on its existing Job owner.

        Attributes:
            action (Literal['remove']):
            actor (str):
            kind (Literal['recipe.cache.remove.v2']):
            recipe_revision_id (str):
            removal_fence (str):
            request_key (str):
            review_digest (str):
            schema_version (Literal[2]):
            selector (str):
            with_model (bool):
     """

    action: Literal['remove']
    actor: str
    kind: Literal['recipe.cache.remove.v2']
    recipe_revision_id: str
    removal_fence: str
    request_key: str
    review_digest: str
    schema_version: Literal[2]
    selector: str
    with_model: bool





    def to_dict(self) -> dict[str, Any]:
        action = self.action

        actor = self.actor

        kind = self.kind

        recipe_revision_id = self.recipe_revision_id

        removal_fence = self.removal_fence

        request_key = self.request_key

        review_digest = self.review_digest

        schema_version = self.schema_version

        selector = self.selector

        with_model = self.with_model


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "action": action,
            "actor": actor,
            "kind": kind,
            "recipe_revision_id": recipe_revision_id,
            "removal_fence": removal_fence,
            "request_key": request_key,
            "review_digest": review_digest,
            "schema_version": schema_version,
            "selector": selector,
            "with_model": with_model,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        action = cast(Literal['remove'] , d.pop("action"))
        if action != 'remove':
            raise ValueError(f"action must match const 'remove', got '{action}'")

        actor = d.pop("actor")

        kind = cast(Literal['recipe.cache.remove.v2'] , d.pop("kind"))
        if kind != 'recipe.cache.remove.v2':
            raise ValueError(f"kind must match const 'recipe.cache.remove.v2', got '{kind}'")

        recipe_revision_id = d.pop("recipe_revision_id")

        removal_fence = d.pop("removal_fence")

        request_key = d.pop("request_key")

        review_digest = d.pop("review_digest")

        schema_version = cast(Literal[2] , d.pop("schema_version"))
        if schema_version != 2:
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        selector = d.pop("selector")

        with_model = d.pop("with_model")

        recipe_cache_removal_intent = cls(
            action=action,
            actor=actor,
            kind=kind,
            recipe_revision_id=recipe_revision_id,
            removal_fence=removal_fence,
            request_key=request_key,
            review_digest=review_digest,
            schema_version=schema_version,
            selector=selector,
            with_model=with_model,
        )

        return recipe_cache_removal_intent
