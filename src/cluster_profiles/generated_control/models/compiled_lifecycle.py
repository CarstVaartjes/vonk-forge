from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="CompiledLifecycle")



@_attrs_define
class CompiledLifecycle:
    """
        Attributes:
            stop_timeout_seconds (int):
     """

    stop_timeout_seconds: int





    def to_dict(self) -> dict[str, Any]:
        stop_timeout_seconds = self.stop_timeout_seconds


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "stop_timeout_seconds": stop_timeout_seconds,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        stop_timeout_seconds = d.pop("stop_timeout_seconds")

        compiled_lifecycle = cls(
            stop_timeout_seconds=stop_timeout_seconds,
        )

        return compiled_lifecycle
