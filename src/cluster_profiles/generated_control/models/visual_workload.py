from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="VisualWorkload")



@_attrs_define
class VisualWorkload:
    """
        Attributes:
            capabilities (list[str]):
            family (str):
     """

    capabilities: list[str]
    family: str





    def to_dict(self) -> dict[str, Any]:
        capabilities = self.capabilities



        family = self.family


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "capabilities": capabilities,
            "family": family,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        capabilities = cast(list[str], d.pop("capabilities"))


        family = d.pop("family")

        visual_workload = cls(
            capabilities=capabilities,
            family=family,
        )

        return visual_workload
