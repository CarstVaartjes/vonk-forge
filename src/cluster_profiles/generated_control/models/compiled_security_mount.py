from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.compiled_security_mount_source import check_compiled_security_mount_source
from ..models.compiled_security_mount_source import CompiledSecurityMountSource
from typing import cast






T = TypeVar("T", bound="CompiledSecurityMount")



@_attrs_define
class CompiledSecurityMount:
    """ Model and input mounts are read-only; the output mount is writable.

        Attributes:
            source (CompiledSecurityMountSource):
            target (str):
     """

    source: CompiledSecurityMountSource
    target: str





    def to_dict(self) -> dict[str, Any]:
        source: str = self.source

        target = self.target


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "source": source,
            "target": target,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        source = check_compiled_security_mount_source(d.pop("source"))




        target = d.pop("target")

        compiled_security_mount = cls(
            source=source,
            target=target,
        )

        return compiled_security_mount
