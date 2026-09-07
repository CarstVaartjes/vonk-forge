from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast, Union






T = TypeVar("T", bound="FailureLogTail")



@_attrs_define
class FailureLogTail:
    """
        Attributes:
            dropped_bytes (Union[None, int]):
            dropped_lines (Union[None, int]):
            text (str):
            truncated (bool):
     """

    dropped_bytes: Union[None, int]
    dropped_lines: Union[None, int]
    text: str
    truncated: bool





    def to_dict(self) -> dict[str, Any]:
        dropped_bytes: Union[None, int]
        dropped_bytes = self.dropped_bytes

        dropped_lines: Union[None, int]
        dropped_lines = self.dropped_lines

        text = self.text

        truncated = self.truncated


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "dropped_bytes": dropped_bytes,
            "dropped_lines": dropped_lines,
            "text": text,
            "truncated": truncated,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        def _parse_dropped_bytes(data: object) -> Union[None, int]:
            if data is None:
                return data
            return cast(Union[None, int], data)

        dropped_bytes = _parse_dropped_bytes(d.pop("dropped_bytes"))


        def _parse_dropped_lines(data: object) -> Union[None, int]:
            if data is None:
                return data
            return cast(Union[None, int], data)

        dropped_lines = _parse_dropped_lines(d.pop("dropped_lines"))


        text = d.pop("text")

        truncated = d.pop("truncated")

        failure_log_tail = cls(
            dropped_bytes=dropped_bytes,
            dropped_lines=dropped_lines,
            text=text,
            truncated=truncated,
        )

        return failure_log_tail
