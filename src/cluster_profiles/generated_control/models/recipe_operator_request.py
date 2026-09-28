from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="RecipeOperatorRequest")



@_attrs_define
class RecipeOperatorRequest:
    """
        Attributes:
            request_key (str):
            with_model (bool):
     """

    request_key: str
    with_model: bool





    def to_dict(self) -> dict[str, Any]:
        request_key = self.request_key

        with_model = self.with_model


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "request_key": request_key,
            "with_model": with_model,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        request_key = d.pop("request_key")

        with_model = d.pop("with_model")

        recipe_operator_request = cls(
            request_key=request_key,
            with_model=with_model,
        )

        return recipe_operator_request
