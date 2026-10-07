from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.observation_transfer_start_resource import check_observation_transfer_start_resource
from ..models.observation_transfer_start_resource import ObservationTransferStartResource
from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="ObservationTransferStart")



@_attrs_define
class ObservationTransferStart:
    """
        Attributes:
            encoding (Literal['base64-canonical-json-utf8-v1']):
            resource (ObservationTransferStartResource):
            transfer_id (str):
            type_ (Literal['start']):
     """

    encoding: Literal['base64-canonical-json-utf8-v1']
    resource: ObservationTransferStartResource
    transfer_id: str
    type_: Literal['start']





    def to_dict(self) -> dict[str, Any]:
        encoding = self.encoding

        resource: str = self.resource

        transfer_id = self.transfer_id

        type_ = self.type_


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "encoding": encoding,
            "resource": resource,
            "transfer_id": transfer_id,
            "type": type_,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        encoding = cast(Literal['base64-canonical-json-utf8-v1'] , d.pop("encoding"))
        if encoding != 'base64-canonical-json-utf8-v1':
            raise ValueError(f"encoding must match const 'base64-canonical-json-utf8-v1', got '{encoding}'")

        resource = check_observation_transfer_start_resource(d.pop("resource"))




        transfer_id = d.pop("transfer_id")

        type_ = cast(Literal['start'] , d.pop("type"))
        if type_ != 'start':
            raise ValueError(f"type must match const 'start', got '{type_}'")

        observation_transfer_start = cls(
            encoding=encoding,
            resource=resource,
            transfer_id=transfer_id,
            type_=type_,
        )

        return observation_transfer_start
