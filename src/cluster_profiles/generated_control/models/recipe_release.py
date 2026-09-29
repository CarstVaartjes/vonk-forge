from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="RecipeRelease")



@_attrs_define
class RecipeRelease:
    """ The version this recipe runs.

    When the upstream project publishes versions, this is the upstream version
    and its release date (for example ``1.6`` released 2026-09-17). A recipe
    whose upstream has no versions carries its own semantic version instead.
    The recipe library's own version follows the contract, not recipe content.

        Attributes:
            released_at (str):
            version (str):
     """

    released_at: str
    version: str





    def to_dict(self) -> dict[str, Any]:
        released_at = self.released_at

        version = self.version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "released_at": released_at,
            "version": version,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        released_at = d.pop("released_at")

        version = d.pop("version")

        recipe_release = cls(
            released_at=released_at,
            version=version,
        )

        return recipe_release
