from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import Literal, cast






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
     """

    ca_fingerprint: str
    controller_endpoint: str
    enrollment_endpoint: str
    expires_at: str
    id: str
    purpose: Literal['new-node']
    token: str





    def to_dict(self) -> dict[str, Any]:
        ca_fingerprint = self.ca_fingerprint

        controller_endpoint = self.controller_endpoint

        enrollment_endpoint = self.enrollment_endpoint

        expires_at = self.expires_at

        id = self.id

        purpose = self.purpose

        token = self.token


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

        enrollment_grant_response = cls(
            ca_fingerprint=ca_fingerprint,
            controller_endpoint=controller_endpoint,
            enrollment_endpoint=enrollment_endpoint,
            expires_at=expires_at,
            id=id,
            purpose=purpose,
            token=token,
        )

        return enrollment_grant_response
