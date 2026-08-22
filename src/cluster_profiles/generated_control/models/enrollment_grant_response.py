from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, cast
from typing import Union






T = TypeVar("T", bound="EnrollmentGrantResponse")



@_attrs_define
class EnrollmentGrantResponse:
    """
        Attributes:
            ca_fingerprint (str):
            controller_endpoint (str):
            enrollment_endpoint (str):
            expires_at (str):
            id (str):
            purpose (Literal['new-node']):
            token (str):
            controller_address (Union[None, Unset, str]):
            service_hostnames (Union[Unset, list[str]]):
     """

    ca_fingerprint: str
    controller_endpoint: str
    enrollment_endpoint: str
    expires_at: str
    id: str
    purpose: Literal['new-node']
    token: str
    controller_address: Union[None, Unset, str] = UNSET
    service_hostnames: Union[Unset, list[str]] = UNSET





    def to_dict(self) -> dict[str, Any]:
        ca_fingerprint = self.ca_fingerprint

        controller_endpoint = self.controller_endpoint

        enrollment_endpoint = self.enrollment_endpoint

        expires_at = self.expires_at

        id = self.id

        purpose = self.purpose

        token = self.token

        controller_address: Union[None, Unset, str]
        if isinstance(self.controller_address, Unset):
            controller_address = UNSET
        else:
            controller_address = self.controller_address

        service_hostnames: Union[Unset, list[str]] = UNSET
        if not isinstance(self.service_hostnames, Unset):
            service_hostnames = self.service_hostnames




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "ca_fingerprint": ca_fingerprint,
            "controller_endpoint": controller_endpoint,
            "enrollment_endpoint": enrollment_endpoint,
            "expires_at": expires_at,
            "id": id,
            "purpose": purpose,
            "token": token,
        })
        if controller_address is not UNSET:
            field_dict["controller_address"] = controller_address
        if service_hostnames is not UNSET:
            field_dict["service_hostnames"] = service_hostnames

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        ca_fingerprint = d.pop("ca_fingerprint")

        controller_endpoint = d.pop("controller_endpoint")

        enrollment_endpoint = d.pop("enrollment_endpoint")

        expires_at = d.pop("expires_at")

        id = d.pop("id")

        purpose = cast(Literal['new-node'] , d.pop("purpose"))
        if purpose != 'new-node':
            raise ValueError(f"purpose must match const 'new-node', got '{purpose}'")

        token = d.pop("token")

        def _parse_controller_address(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        controller_address = _parse_controller_address(d.pop("controller_address", UNSET))


        service_hostnames = cast(list[str], d.pop("service_hostnames", UNSET))


        enrollment_grant_response = cls(
            ca_fingerprint=ca_fingerprint,
            controller_endpoint=controller_endpoint,
            enrollment_endpoint=enrollment_endpoint,
            expires_at=expires_at,
            id=id,
            purpose=purpose,
            token=token,
            controller_address=controller_address,
            service_hostnames=service_hostnames,
        )

        return enrollment_grant_response
