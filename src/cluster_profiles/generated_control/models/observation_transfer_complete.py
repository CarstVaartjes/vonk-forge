from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import Literal, cast






T = TypeVar("T", bound="ObservationTransferComplete")



@_attrs_define
class ObservationTransferComplete:
    """
        Attributes:
            bytes_ (int):
            chunks (int):
            sha256 (str):
            transfer_id (str):
            type_ (Literal['complete']):
     """

    bytes_: int
    chunks: int
    sha256: str
    transfer_id: str
    type_: Literal['complete']





    def to_dict(self) -> dict[str, Any]:
        bytes_ = self.bytes_

        chunks = self.chunks

        sha256 = self.sha256

        transfer_id = self.transfer_id

        type_ = self.type_


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "bytes": bytes_,
            "chunks": chunks,
            "sha256": sha256,
            "transfer_id": transfer_id,
            "type": type_,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        bytes_ = d.pop("bytes")

        chunks = d.pop("chunks")

        sha256 = d.pop("sha256")

        transfer_id = d.pop("transfer_id")

        type_ = cast(Literal['complete'] , d.pop("type"))
        if type_ != 'complete':
            raise ValueError(f"type must match const 'complete', got '{type_}'")

        observation_transfer_complete = cls(
            bytes_=bytes_,
            chunks=chunks,
            sha256=sha256,
            transfer_id=transfer_id,
            type_=type_,
        )

        return observation_transfer_complete
