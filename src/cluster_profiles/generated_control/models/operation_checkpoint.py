from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="OperationCheckpoint")



@_attrs_define
class OperationCheckpoint:
    """ Restart-safe cursor identifying a durable operation unit.

        Attributes:
            key (str):
            sequence (int):
            cursor (None | str | Unset):
            digest (None | str | Unset):
     """

    key: str
    sequence: int
    cursor: None | str | Unset = UNSET
    digest: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        key = self.key

        sequence = self.sequence

        cursor: None | str | Unset
        if isinstance(self.cursor, Unset):
            cursor = UNSET
        else:
            cursor = self.cursor

        digest: None | str | Unset
        if isinstance(self.digest, Unset):
            digest = UNSET
        else:
            digest = self.digest


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "key": key,
            "sequence": sequence,
        })
        if cursor is not UNSET:
            field_dict["cursor"] = cursor
        if digest is not UNSET:
            field_dict["digest"] = digest

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        key = d.pop("key")

        sequence = d.pop("sequence")

        def _parse_cursor(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        cursor = _parse_cursor(d.pop("cursor", UNSET))


        def _parse_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        digest = _parse_digest(d.pop("digest", UNSET))


        operation_checkpoint = cls(
            key=key,
            sequence=sequence,
            cursor=cursor,
            digest=digest,
        )

        return operation_checkpoint
