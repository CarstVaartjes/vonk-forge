from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="PrebuiltImage")



@_attrs_define
class PrebuiltImage:
    """ One catalog-pinned prebuilt runtime image for a recipe revision.

        Attributes:
            build_key (str):
            reference (str):
     """

    build_key: str
    reference: str





    def to_dict(self) -> dict[str, Any]:
        build_key = self.build_key

        reference = self.reference


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "build_key": build_key,
            "reference": reference,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        build_key = d.pop("build_key")

        reference = d.pop("reference")

        prebuilt_image = cls(
            build_key=build_key,
            reference=reference,
        )

        return prebuilt_image
