from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast
import datetime

if TYPE_CHECKING:
  from ..models.recipe_operation_cancellation_result import RecipeOperationCancellationResult
  from ..models.recipe_update_child import RecipeUpdateChild
  from ..models.recipe_update_scope import RecipeUpdateScope





T = TypeVar("T", bound="RecipeUpdateDocument")



@_attrs_define
class RecipeUpdateDocument:
    """
        Attributes:
            children (list[RecipeUpdateChild]):
            request (RecipeUpdateScope):
            cancellation (None | RecipeOperationCancellationResult | Unset):
            claim_owner (None | str | Unset):
            claim_until (datetime.datetime | None | Unset):
            kind (Literal['recipe.cache.update.v2'] | Unset):  Default: 'recipe.cache.update.v2'.
            next_attempt_at (datetime.datetime | None | Unset):
            next_child (int | Unset):  Default: 0.
            schema_version (Literal[2] | Unset):  Default: 2.
     """

    children: list[RecipeUpdateChild]
    request: RecipeUpdateScope
    cancellation: None | RecipeOperationCancellationResult | Unset = UNSET
    claim_owner: None | str | Unset = UNSET
    claim_until: datetime.datetime | None | Unset = UNSET
    kind: Literal['recipe.cache.update.v2'] | Unset = 'recipe.cache.update.v2'
    next_attempt_at: datetime.datetime | None | Unset = UNSET
    next_child: int | Unset = 0
    schema_version: Literal[2] | Unset = 2
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_operation_cancellation_result import RecipeOperationCancellationResult # noqa: PLC0415
        from ..models.recipe_update_child import RecipeUpdateChild # noqa: PLC0415
        from ..models.recipe_update_scope import RecipeUpdateScope # noqa: PLC0415
        children = []
        for children_item_data in self.children:
            children_item = children_item_data.to_dict()
            children.append(children_item)



        request = self.request.to_dict()

        cancellation: dict[str, Any] | None | Unset
        if isinstance(self.cancellation, Unset):
            cancellation = UNSET
        elif isinstance(self.cancellation, RecipeOperationCancellationResult):
            cancellation = self.cancellation.to_dict()
        else:
            cancellation = self.cancellation

        claim_owner: None | str | Unset
        if isinstance(self.claim_owner, Unset):
            claim_owner = UNSET
        else:
            claim_owner = self.claim_owner

        claim_until: None | str | Unset
        if isinstance(self.claim_until, Unset):
            claim_until = UNSET
        elif isinstance(self.claim_until, datetime.datetime):
            claim_until = self.claim_until.isoformat()
        else:
            claim_until = self.claim_until

        kind = self.kind

        next_attempt_at: None | str | Unset
        if isinstance(self.next_attempt_at, Unset):
            next_attempt_at = UNSET
        elif isinstance(self.next_attempt_at, datetime.datetime):
            next_attempt_at = self.next_attempt_at.isoformat()
        else:
            next_attempt_at = self.next_attempt_at

        next_child = self.next_child

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "children": children,
            "request": request,
        })
        if cancellation is not UNSET:
            field_dict["cancellation"] = cancellation
        if claim_owner is not UNSET:
            field_dict["claim_owner"] = claim_owner
        if claim_until is not UNSET:
            field_dict["claim_until"] = claim_until
        if kind is not UNSET:
            field_dict["kind"] = kind
        if next_attempt_at is not UNSET:
            field_dict["next_attempt_at"] = next_attempt_at
        if next_child is not UNSET:
            field_dict["next_child"] = next_child
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_operation_cancellation_result import RecipeOperationCancellationResult # noqa: PLC0415
        from ..models.recipe_update_child import RecipeUpdateChild # noqa: PLC0415
        from ..models.recipe_update_scope import RecipeUpdateScope # noqa: PLC0415
        d = dict(src_dict)
        children = []
        _children = d.pop("children")
        for children_item_data in (_children):
            children_item = RecipeUpdateChild.from_dict(children_item_data)



            children.append(children_item)


        request = RecipeUpdateScope.from_dict(d.pop("request"))




        def _parse_cancellation(data: object) -> None | RecipeOperationCancellationResult | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                cancellation_type_0 = RecipeOperationCancellationResult.from_dict(data)



                return cancellation_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RecipeOperationCancellationResult | Unset, data)

        cancellation = _parse_cancellation(d.pop("cancellation", UNSET))


        def _parse_claim_owner(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        claim_owner = _parse_claim_owner(d.pop("claim_owner", UNSET))


        def _parse_claim_until(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                claim_until_type_0 = datetime.datetime.fromisoformat(data)



                return claim_until_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        claim_until = _parse_claim_until(d.pop("claim_until", UNSET))


        kind = cast(Literal['recipe.cache.update.v2'] | Unset , d.pop("kind", UNSET))
        if kind != 'recipe.cache.update.v2' and not isinstance(kind, Unset):
            raise ValueError(f"kind must match const 'recipe.cache.update.v2', got '{kind}'")

        def _parse_next_attempt_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                next_attempt_at_type_0 = datetime.datetime.fromisoformat(data)



                return next_attempt_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        next_attempt_at = _parse_next_attempt_at(d.pop("next_attempt_at", UNSET))


        next_child = d.pop("next_child", UNSET)

        schema_version = cast(Literal[2] | Unset , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        recipe_update_document = cls(
            children=children,
            request=request,
            cancellation=cancellation,
            claim_owner=claim_owner,
            claim_until=claim_until,
            kind=kind,
            next_attempt_at=next_attempt_at,
            next_child=next_child,
            schema_version=schema_version,
        )


        recipe_update_document.additional_properties = d
        return recipe_update_document

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
