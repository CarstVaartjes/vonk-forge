from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="ModelFile")



@_attrs_define
class ModelFile:
    """ One entry in the complete immutable model file manifest.

        Attributes:
            id (str):
            path (str):
            roles (list[str]):
            sha256 (str):
            size_bytes (int):
     """

    id: str
    path: str
    roles: list[str]
    sha256: str
    size_bytes: int





    def to_dict(self) -> dict[str, Any]:
        id = self.id

        path = self.path

        roles = self.roles



        sha256 = self.sha256

        size_bytes = self.size_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "id": id,
            "path": path,
            "roles": roles,
            "sha256": sha256,
            "size_bytes": size_bytes,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        path = d.pop("path")

        roles = cast(list[str], d.pop("roles"))


        sha256 = d.pop("sha256")

        size_bytes = d.pop("size_bytes")

        model_file = cls(
            id=id,
            path=path,
            roles=roles,
            sha256=sha256,
            size_bytes=size_bytes,
        )

        return model_file
