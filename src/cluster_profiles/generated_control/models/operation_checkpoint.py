from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="OperationCheckpoint")



@_attrs_define
class OperationCheckpoint:
    """ A restart-safe cursor identifying the last completed durable unit.

        Attributes:
            key (str):
            sequence (int):
            cursor (Union[None, Unset, str]):
            digest (Union[None, Unset, str]):
     """

    key: str
    sequence: int
    cursor: Union[None, Unset, str] = UNSET
    digest: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        key = self.key

        sequence = self.sequence

        cursor: Union[None, Unset, str]
        if isinstance(self.cursor, Unset):
            cursor = UNSET
        else:
            cursor = self.cursor

        digest: Union[None, Unset, str]
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

        def _parse_cursor(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        cursor = _parse_cursor(d.pop("cursor", UNSET))


        def _parse_digest(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        digest = _parse_digest(d.pop("digest", UNSET))


        operation_checkpoint = cls(
            key=key,
            sequence=sequence,
            cursor=cursor,
            digest=digest,
        )

        return operation_checkpoint
