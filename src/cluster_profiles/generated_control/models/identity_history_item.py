from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
import datetime






T = TypeVar("T", bound="IdentityHistoryItem")



@_attrs_define
class IdentityHistoryItem:
    """
        Attributes:
            agent_state (str):
            node_id (str):
            certificate_fingerprint (None | str | Unset):
            certificate_generation (int | None | Unset):
            certificate_serial (None | str | Unset):
            enrolled_at (datetime.datetime | None | Unset):
            revoked_at (datetime.datetime | None | Unset):
     """

    agent_state: str
    node_id: str
    certificate_fingerprint: None | str | Unset = UNSET
    certificate_generation: int | None | Unset = UNSET
    certificate_serial: None | str | Unset = UNSET
    enrolled_at: datetime.datetime | None | Unset = UNSET
    revoked_at: datetime.datetime | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        agent_state = self.agent_state

        node_id = self.node_id

        certificate_fingerprint: None | str | Unset
        if isinstance(self.certificate_fingerprint, Unset):
            certificate_fingerprint = UNSET
        else:
            certificate_fingerprint = self.certificate_fingerprint

        certificate_generation: int | None | Unset
        if isinstance(self.certificate_generation, Unset):
            certificate_generation = UNSET
        else:
            certificate_generation = self.certificate_generation

        certificate_serial: None | str | Unset
        if isinstance(self.certificate_serial, Unset):
            certificate_serial = UNSET
        else:
            certificate_serial = self.certificate_serial

        enrolled_at: None | str | Unset
        if isinstance(self.enrolled_at, Unset):
            enrolled_at = UNSET
        elif isinstance(self.enrolled_at, datetime.datetime):
            enrolled_at = self.enrolled_at.isoformat()
        else:
            enrolled_at = self.enrolled_at

        revoked_at: None | str | Unset
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

        def _parse_certificate_fingerprint(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        certificate_fingerprint = _parse_certificate_fingerprint(d.pop("certificate_fingerprint", UNSET))


        def _parse_certificate_generation(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        certificate_generation = _parse_certificate_generation(d.pop("certificate_generation", UNSET))


        def _parse_certificate_serial(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        certificate_serial = _parse_certificate_serial(d.pop("certificate_serial", UNSET))


        def _parse_enrolled_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                enrolled_at_type_0 = datetime.datetime.fromisoformat(data)



                return enrolled_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        enrolled_at = _parse_enrolled_at(d.pop("enrolled_at", UNSET))


        def _parse_revoked_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                revoked_at_type_0 = datetime.datetime.fromisoformat(data)



                return revoked_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

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
