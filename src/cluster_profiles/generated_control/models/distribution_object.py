from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import Literal, cast






T = TypeVar("T", bound="DistributionObject")



@_attrs_define
class DistributionObject:
    """ One model file referenced by an assignment.

        Attributes:
            bytes_ (int):
            kind (Literal['model']):
            name (str):
            sha256 (str):
     """

    bytes_: int
    kind: Literal['model']
    name: str
    sha256: str





    def to_dict(self) -> dict[str, Any]:
        bytes_ = self.bytes_

        kind = self.kind

        name = self.name

        sha256 = self.sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "bytes": bytes_,
            "kind": kind,
            "name": name,
            "sha256": sha256,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        bytes_ = d.pop("bytes")

        kind = cast(Literal['model'] , d.pop("kind"))
        if kind != 'model':
            raise ValueError(f"kind must match const 'model', got '{kind}'")

        name = d.pop("name")

        sha256 = d.pop("sha256")

        distribution_object = cls(
            bytes_=bytes_,
            kind=kind,
            name=name,
            sha256=sha256,
        )

        return distribution_object
