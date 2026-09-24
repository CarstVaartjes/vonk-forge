from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="OperationOwnerReference")



@_attrs_define
class OperationOwnerReference:
    """ Exact durable owner and original request identity for Activity.

        Attributes:
            id (str):
            kind (str):
            request_id (Union[None, Unset, str]):
     """

    id: str
    kind: str
    request_id: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        id = self.id

        kind = self.kind

        request_id: Union[None, Unset, str]
        if isinstance(self.request_id, Unset):
            request_id = UNSET
        else:
            request_id = self.request_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "id": id,
            "kind": kind,
        })
        if request_id is not UNSET:
            field_dict["request_id"] = request_id

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        kind = d.pop("kind")

        def _parse_request_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        request_id = _parse_request_id(d.pop("request_id", UNSET))


        operation_owner_reference = cls(
            id=id,
            kind=kind,
            request_id=request_id,
        )

        return operation_owner_reference
