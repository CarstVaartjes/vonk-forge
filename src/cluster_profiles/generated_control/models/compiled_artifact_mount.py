from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="CompiledArtifactMount")



@_attrs_define
class CompiledArtifactMount:
    """ A read-only model mount.

        Attributes:
            target (str):
     """

    target: str





    def to_dict(self) -> dict[str, Any]:
        target = self.target


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "target": target,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        target = d.pop("target")

        compiled_artifact_mount = cls(
            target=target,
        )

        return compiled_artifact_mount
