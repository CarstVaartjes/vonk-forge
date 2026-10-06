from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="ArtifactInputProjection")



@_attrs_define
class ArtifactInputProjection:
    """
        Attributes:
            artifact_key (str):
            selection_id (str):
     """

    artifact_key: str
    selection_id: str





    def to_dict(self) -> dict[str, Any]:
        artifact_key = self.artifact_key

        selection_id = self.selection_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifact_key": artifact_key,
            "selection_id": selection_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        artifact_key = d.pop("artifact_key")

        selection_id = d.pop("selection_id")

        artifact_input_projection = cls(
            artifact_key=artifact_key,
            selection_id=selection_id,
        )

        return artifact_input_projection
