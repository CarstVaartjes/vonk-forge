from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.compiled_distribution_object_kind import check_compiled_distribution_object_kind
from ..models.compiled_distribution_object_kind import CompiledDistributionObjectKind
from typing import cast






T = TypeVar("T", bound="CompiledDistributionObject")



@_attrs_define
class CompiledDistributionObject:
    """ Distribution objects usable as installed model or runtime inputs.

        Attributes:
            bytes_ (int):
            kind (CompiledDistributionObjectKind):
            name (str):
            sha256 (str):
     """

    bytes_: int
    kind: CompiledDistributionObjectKind
    name: str
    sha256: str





    def to_dict(self) -> dict[str, Any]:
        bytes_ = self.bytes_

        kind: str = self.kind

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

        kind = check_compiled_distribution_object_kind(d.pop("kind"))




        name = d.pop("name")

        sha256 = d.pop("sha256")

        compiled_distribution_object = cls(
            bytes_=bytes_,
            kind=kind,
            name=name,
            sha256=sha256,
        )

        return compiled_distribution_object
