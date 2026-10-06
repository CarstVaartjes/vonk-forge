from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="RecipeBuildCleanupRequest")



@_attrs_define
class RecipeBuildCleanupRequest:
    """ Stop only the transient service belonging to one retained build attempt.

        Attributes:
            build_id (str):
            operation_id (str):
     """

    build_id: str
    operation_id: str





    def to_dict(self) -> dict[str, Any]:
        build_id = self.build_id

        operation_id = self.operation_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "build_id": build_id,
            "operation_id": operation_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        build_id = d.pop("build_id")

        operation_id = d.pop("operation_id")

        recipe_build_cleanup_request = cls(
            build_id=build_id,
            operation_id=operation_id,
        )

        return recipe_build_cleanup_request
