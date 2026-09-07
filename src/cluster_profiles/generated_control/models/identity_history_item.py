from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Union
import datetime






T = TypeVar("T", bound="IdentityHistoryItem")



@_attrs_define
class IdentityHistoryItem:
    """
        Attributes:
            agent_state (str):
            node_id (str):
            certificate_fingerprint (Union[None, Unset, str]):
            certificate_generation (Union[None, Unset, int]):
            certificate_serial (Union[None, Unset, str]):
            enrolled_at (Union[None, Unset, datetime.datetime]):
            revoked_at (Union[None, Unset, datetime.datetime]):
     """

    agent_state: str
    node_id: str
    certificate_fingerprint: Union[None, Unset, str] = UNSET
    certificate_generation: Union[None, Unset, int] = UNSET
    certificate_serial: Union[None, Unset, str] = UNSET
    enrolled_at: Union[None, Unset, datetime.datetime] = UNSET
    revoked_at: Union[None, Unset, datetime.datetime] = UNSET





    def to_dict(self) -> dict[str, Any]:
        agent_state = self.agent_state

        node_id = self.node_id

        certificate_fingerprint: Union[None, Unset, str]
        if isinstance(self.certificate_fingerprint, Unset):
            certificate_fingerprint = UNSET
        else:
            certificate_fingerprint = self.certificate_fingerprint

        certificate_generation: Union[None, Unset, int]
        if isinstance(self.certificate_generation, Unset):
            certificate_generation = UNSET
        else:
            certificate_generation = self.certificate_generation

        certificate_serial: Union[None, Unset, str]
        if isinstance(self.certificate_serial, Unset):
            certificate_serial = UNSET
        else:
            certificate_serial = self.certificate_serial

        enrolled_at: Union[None, Unset, str]
        if isinstance(self.enrolled_at, Unset):
            enrolled_at = UNSET
        elif isinstance(self.enrolled_at, datetime.datetime):
            enrolled_at = self.enrolled_at.isoformat()
        else:
            enrolled_at = self.enrolled_at

        revoked_at: Union[None, Unset, str]
        if isinstance(self.revoked_at, Unset):
            revoked_at = UNSET
        elif isinstance(self.revoked_at, datetime.datetime):
            revoked_at = self.revoked_at.isoformat()
        else:
            revoked_at = self.revoked_at


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "agent_state": agent_state,
            "node_id": node_id,
        })
        if certificate_fingerprint is not UNSET:
            field_dict["certificate_fingerprint"] = certificate_fingerprint
        if certificate_generation is not UNSET:
            field_dict["certificate_generation"] = certificate_generation
        if certificate_serial is not UNSET:
            field_dict["certificate_serial"] = certificate_serial
        if enrolled_at is not UNSET:
            field_dict["enrolled_at"] = enrolled_at
        if revoked_at is not UNSET:
            field_dict["revoked_at"] = revoked_at

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        agent_state = d.pop("agent_state")

        node_id = d.pop("node_id")

        def _parse_certificate_fingerprint(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        certificate_fingerprint = _parse_certificate_fingerprint(d.pop("certificate_fingerprint", UNSET))


        def _parse_certificate_generation(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        certificate_generation = _parse_certificate_generation(d.pop("certificate_generation", UNSET))


        def _parse_certificate_serial(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        certificate_serial = _parse_certificate_serial(d.pop("certificate_serial", UNSET))


        def _parse_enrolled_at(data: object) -> Union[None, Unset, datetime.datetime]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                enrolled_at_type_0 = isoparse(data)



                return enrolled_at_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, Unset, datetime.datetime], data)

        enrolled_at = _parse_enrolled_at(d.pop("enrolled_at", UNSET))


        def _parse_revoked_at(data: object) -> Union[None, Unset, datetime.datetime]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                revoked_at_type_0 = isoparse(data)



                return revoked_at_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, Unset, datetime.datetime], data)

        revoked_at = _parse_revoked_at(d.pop("revoked_at", UNSET))


        identity_history_item = cls(
            agent_state=agent_state,
            node_id=node_id,
            certificate_fingerprint=certificate_fingerprint,
            certificate_generation=certificate_generation,
            certificate_serial=certificate_serial,
            enrolled_at=enrolled_at,
            revoked_at=revoked_at,
        )

        return identity_history_item
