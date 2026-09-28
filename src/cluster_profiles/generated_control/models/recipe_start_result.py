from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="RecipeStartResult")



@_attrs_define
class RecipeStartResult:
    """ The serving rank reports its endpoint; every other rank reports ``{}``.

        Attributes:
            endpoint (None | str | Unset):
     """

    endpoint: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        endpoint: None | str | Unset
        if isinstance(self.endpoint, Unset):
            endpoint = UNSET
        else:
            endpoint = self.endpoint


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if endpoint is not UNSET:
            field_dict["endpoint"] = endpoint

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        def _parse_endpoint(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        endpoint = _parse_endpoint(d.pop("endpoint", UNSET))


        recipe_start_result = cls(
            endpoint=endpoint,
        )

        return recipe_start_result
