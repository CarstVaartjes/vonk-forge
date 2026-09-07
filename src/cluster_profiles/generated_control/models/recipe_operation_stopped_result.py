from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="RecipeOperationStoppedResult")



@_attrs_define
class RecipeOperationStoppedResult:
    """
        Attributes:
            stopped (bool):
     """

    stopped: bool





    def to_dict(self) -> dict[str, Any]:
        stopped = self.stopped


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "stopped": stopped,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        stopped = d.pop("stopped")

        recipe_operation_stopped_result = cls(
            stopped=stopped,
        )

        return recipe_operation_stopped_result
