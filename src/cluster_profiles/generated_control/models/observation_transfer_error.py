from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import Literal, cast






T = TypeVar("T", bound="ObservationTransferError")



@_attrs_define
class ObservationTransferError:
    """
        Attributes:
            detail (str):
            reason_code (Literal['observation.transfer_unavailable']):
            transfer_id (str):
            type_ (Literal['error']):
     """

    detail: str
    reason_code: Literal['observation.transfer_unavailable']
    transfer_id: str
    type_: Literal['error']





    def to_dict(self) -> dict[str, Any]:
        detail = self.detail

        reason_code = self.reason_code

        transfer_id = self.transfer_id

        type_ = self.type_


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "detail": detail,
            "reason_code": reason_code,
            "transfer_id": transfer_id,
            "type": type_,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        detail = d.pop("detail")

        reason_code = cast(Literal['observation.transfer_unavailable'] , d.pop("reason_code"))
        if reason_code != 'observation.transfer_unavailable':
            raise ValueError(f"reason_code must match const 'observation.transfer_unavailable', got '{reason_code}'")

        transfer_id = d.pop("transfer_id")

        type_ = cast(Literal['error'] , d.pop("type"))
        if type_ != 'error':
            raise ValueError(f"type must match const 'error', got '{type_}'")

        observation_transfer_error = cls(
            detail=detail,
            reason_code=reason_code,
            transfer_id=transfer_id,
            type_=type_,
        )

        return observation_transfer_error
