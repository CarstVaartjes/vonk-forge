from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="RecipeImageAvailabilityRetry")



@_attrs_define
class RecipeImageAvailabilityRetry:
    """
        Attributes:
            request_key (str):
     """

    request_key: str





    def to_dict(self) -> dict[str, Any]:
        request_key = self.request_key


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "request_key": request_key,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        request_key = d.pop("request_key")

        recipe_image_availability_retry = cls(
            request_key=request_key,
        )

        return recipe_image_availability_retry
