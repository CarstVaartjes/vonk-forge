from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="WritablePath")



@_attrs_define
class WritablePath:
    """
        Attributes:
            name (str):
            path (str):
            persistent (bool):
     """

    name: str
    path: str
    persistent: bool





    def to_dict(self) -> dict[str, Any]:
        name = self.name

        path = self.path

        persistent = self.persistent


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "name": name,
            "path": path,
            "persistent": persistent,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        name = d.pop("name")

        path = d.pop("path")

        persistent = d.pop("persistent")

        writable_path = cls(
            name=name,
            path=path,
            persistent=persistent,
        )

        return writable_path
