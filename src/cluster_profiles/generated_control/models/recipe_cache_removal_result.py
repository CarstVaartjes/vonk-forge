from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="RecipeCacheRemovalResult")



@_attrs_define
class RecipeCacheRemovalResult:
    """ Stored terminal summary, bound to the Job's immutable intent.

        Attributes:
            action (Literal['remove']):
            cancelled_builds (list[str]):
            cancelled_operations (list[str]):
            model_removals (list[str]):
            next_actions (list[str]):
            operation_id (str):
            preserved (list[str]):
            recipe_revision_id (str):
            reclaimed_bytes (int):
            request_key (str):
            review_digest (str):
            schema_version (Literal[2]):
            selector (str):
            state (Literal['succeeded']):
            with_model (bool):
     """

    action: Literal['remove']
    cancelled_builds: list[str]
    cancelled_operations: list[str]
    model_removals: list[str]
    next_actions: list[str]
    operation_id: str
    preserved: list[str]
    recipe_revision_id: str
    reclaimed_bytes: int
    request_key: str
    review_digest: str
    schema_version: Literal[2]
    selector: str
    state: Literal['succeeded']
    with_model: bool





    def to_dict(self) -> dict[str, Any]:
        action = self.action

        cancelled_builds = self.cancelled_builds



        cancelled_operations = self.cancelled_operations



        model_removals = self.model_removals



        next_actions = self.next_actions



        operation_id = self.operation_id

        preserved = self.preserved



        recipe_revision_id = self.recipe_revision_id

        reclaimed_bytes = self.reclaimed_bytes

        request_key = self.request_key

        review_digest = self.review_digest

        schema_version = self.schema_version

        selector = self.selector

        state = self.state

        with_model = self.with_model


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "action": action,
            "cancelled_builds": cancelled_builds,
            "cancelled_operations": cancelled_operations,
            "model_removals": model_removals,
            "next_actions": next_actions,
            "operation_id": operation_id,
            "preserved": preserved,
            "recipe_revision_id": recipe_revision_id,
            "reclaimed_bytes": reclaimed_bytes,
            "request_key": request_key,
            "review_digest": review_digest,
            "schema_version": schema_version,
            "selector": selector,
            "state": state,
            "with_model": with_model,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        action = cast(Literal['remove'] , d.pop("action"))
        if action != 'remove':
            raise ValueError(f"action must match const 'remove', got '{action}'")

        cancelled_builds = cast(list[str], d.pop("cancelled_builds"))


        cancelled_operations = cast(list[str], d.pop("cancelled_operations"))


        model_removals = cast(list[str], d.pop("model_removals"))


        next_actions = cast(list[str], d.pop("next_actions"))


        operation_id = d.pop("operation_id")

        preserved = cast(list[str], d.pop("preserved"))


        recipe_revision_id = d.pop("recipe_revision_id")

        reclaimed_bytes = d.pop("reclaimed_bytes")

        request_key = d.pop("request_key")

        review_digest = d.pop("review_digest")

        schema_version = cast(Literal[2] , d.pop("schema_version"))
        if schema_version != 2:
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        selector = d.pop("selector")

        state = cast(Literal['succeeded'] , d.pop("state"))
        if state != 'succeeded':
            raise ValueError(f"state must match const 'succeeded', got '{state}'")

        with_model = d.pop("with_model")

        recipe_cache_removal_result = cls(
            action=action,
            cancelled_builds=cancelled_builds,
            cancelled_operations=cancelled_operations,
            model_removals=model_removals,
            next_actions=next_actions,
            operation_id=operation_id,
            preserved=preserved,
            recipe_revision_id=recipe_revision_id,
            reclaimed_bytes=reclaimed_bytes,
            request_key=request_key,
            review_digest=review_digest,
            schema_version=schema_version,
            selector=selector,
            state=state,
            with_model=with_model,
        )

        return recipe_cache_removal_result
