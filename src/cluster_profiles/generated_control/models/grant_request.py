from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.grant_request_purpose import check_grant_request_purpose
from ..models.grant_request_purpose import GrantRequestPurpose
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="GrantRequest")



@_attrs_define
class GrantRequest:
    """
        Attributes:
            ttl_seconds (int):
            node_id (Union[None, Unset, str]):
            purpose (Union[Unset, GrantRequestPurpose]):  Default: 'new-node'.
     """

    ttl_seconds: int
    node_id: Union[None, Unset, str] = UNSET
    purpose: Union[Unset, GrantRequestPurpose] = 'new-node'





    def to_dict(self) -> dict[str, Any]:
        ttl_seconds = self.ttl_seconds

        node_id: Union[None, Unset, str]
        if isinstance(self.node_id, Unset):
            node_id = UNSET
        else:
            node_id = self.node_id

        purpose: Union[Unset, str] = UNSET
        if not isinstance(self.purpose, Unset):
            purpose = self.purpose



        field_dict: dict[str, Any] = {}

        field_dict.update({
            "ttl_seconds": ttl_seconds,
        })
        if node_id is not UNSET:
            field_dict["node_id"] = node_id
        if purpose is not UNSET:
            field_dict["purpose"] = purpose

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        ttl_seconds = d.pop("ttl_seconds")

        def _parse_node_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        node_id = _parse_node_id(d.pop("node_id", UNSET))


        _purpose = d.pop("purpose", UNSET)
        purpose: Union[Unset, GrantRequestPurpose]
        if isinstance(_purpose,  Unset):
            purpose = UNSET
        else:
            purpose = check_grant_request_purpose(_purpose)




        grant_request = cls(
            ttl_seconds=ttl_seconds,
            node_id=node_id,
            purpose=purpose,
        )

        return grant_request
