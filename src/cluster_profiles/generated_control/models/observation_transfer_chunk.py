from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import Literal, cast






T = TypeVar("T", bound="ObservationTransferChunk")



@_attrs_define
class ObservationTransferChunk:
    """
        Attributes:
            data (str):
            ordinal (int):
            transfer_id (str):
            type_ (Literal['chunk']):
     """

    data: str
    ordinal: int
    transfer_id: str
    type_: Literal['chunk']





    def to_dict(self) -> dict[str, Any]:
        data = self.data

        ordinal = self.ordinal

        transfer_id = self.transfer_id

        type_ = self.type_


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "data": data,
            "ordinal": ordinal,
            "transfer_id": transfer_id,
            "type": type_,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        data = d.pop("data")

        ordinal = d.pop("ordinal")

        transfer_id = d.pop("transfer_id")

        type_ = cast(Literal['chunk'] , d.pop("type"))
        if type_ != 'chunk':
            raise ValueError(f"type must match const 'chunk', got '{type_}'")

        observation_transfer_chunk = cls(
            data=data,
            ordinal=ordinal,
            transfer_id=transfer_id,
            type_=type_,
        )

        return observation_transfer_chunk
