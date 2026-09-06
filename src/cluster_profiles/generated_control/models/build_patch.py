from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="BuildPatch")



@_attrs_define
class BuildPatch:
    """
        Attributes:
            path (str):
     """

    path: str





    def to_dict(self) -> dict[str, Any]:
        path = self.path


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "path": path,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        path = d.pop("path")

        build_patch = cls(
            path=path,
        )

        return build_patch
