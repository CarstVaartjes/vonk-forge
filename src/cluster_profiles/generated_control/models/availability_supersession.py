from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
import datetime






T = TypeVar("T", bound="AvailabilitySupersession")



@_attrs_define
class AvailabilitySupersession:
    """
        Attributes:
            code (str):
            recipe_revision_id (str):
            superseded_at (datetime.datetime):
     """

    code: str
    recipe_revision_id: str
    superseded_at: datetime.datetime





    def to_dict(self) -> dict[str, Any]:
        code = self.code

        recipe_revision_id = self.recipe_revision_id

        superseded_at = self.superseded_at.isoformat()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "code": code,
            "recipe_revision_id": recipe_revision_id,
            "superseded_at": superseded_at,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        code = d.pop("code")

        recipe_revision_id = d.pop("recipe_revision_id")

        superseded_at = datetime.datetime.fromisoformat(d.pop("superseded_at"))




        availability_supersession = cls(
            code=code,
            recipe_revision_id=recipe_revision_id,
            superseded_at=superseded_at,
        )

        return availability_supersession
