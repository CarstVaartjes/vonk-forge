from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="EnrollmentSummary")



@_attrs_define
class EnrollmentSummary:
    """
        Attributes:
            agent_digest (str):
            boot_id (str):
            created_at (str):
            csr_public_key_fingerprint (str):
            hardware_fingerprint (str):
            host_key_fingerprint (str):
            id (str):
            node_id (str):
            state (str):
            certificate_fingerprint (Union[None, Unset, str]):
            certificate_serial (Union[None, Unset, str]):
            decided_at (Union[None, Unset, str]):
            decision_actor (Union[None, Unset, str]):
     """

    agent_digest: str
    boot_id: str
    created_at: str
    csr_public_key_fingerprint: str
    hardware_fingerprint: str
    host_key_fingerprint: str
    id: str
    node_id: str
    state: str
    certificate_fingerprint: Union[None, Unset, str] = UNSET
    certificate_serial: Union[None, Unset, str] = UNSET
    decided_at: Union[None, Unset, str] = UNSET
    decision_actor: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        agent_digest = self.agent_digest

        boot_id = self.boot_id

        created_at = self.created_at

        csr_public_key_fingerprint = self.csr_public_key_fingerprint

        hardware_fingerprint = self.hardware_fingerprint

        host_key_fingerprint = self.host_key_fingerprint

        id = self.id

        node_id = self.node_id

        state = self.state

        certificate_fingerprint: Union[None, Unset, str]
        if isinstance(self.certificate_fingerprint, Unset):
            certificate_fingerprint = UNSET
        else:
            certificate_fingerprint = self.certificate_fingerprint

        certificate_serial: Union[None, Unset, str]
        if isinstance(self.certificate_serial, Unset):
            certificate_serial = UNSET
        else:
            certificate_serial = self.certificate_serial

        decided_at: Union[None, Unset, str]
        if isinstance(self.decided_at, Unset):
            decided_at = UNSET
        else:
            decided_at = self.decided_at

        decision_actor: Union[None, Unset, str]
        if isinstance(self.decision_actor, Unset):
            decision_actor = UNSET
        else:
            decision_actor = self.decision_actor


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "agent_digest": agent_digest,
            "boot_id": boot_id,
            "created_at": created_at,
            "csr_public_key_fingerprint": csr_public_key_fingerprint,
            "hardware_fingerprint": hardware_fingerprint,
            "host_key_fingerprint": host_key_fingerprint,
            "id": id,
            "node_id": node_id,
            "state": state,
        })
        if certificate_fingerprint is not UNSET:
            field_dict["certificate_fingerprint"] = certificate_fingerprint
        if certificate_serial is not UNSET:
            field_dict["certificate_serial"] = certificate_serial
        if decided_at is not UNSET:
            field_dict["decided_at"] = decided_at
        if decision_actor is not UNSET:
            field_dict["decision_actor"] = decision_actor

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        agent_digest = d.pop("agent_digest")

        boot_id = d.pop("boot_id")

        created_at = d.pop("created_at")

        csr_public_key_fingerprint = d.pop("csr_public_key_fingerprint")

        hardware_fingerprint = d.pop("hardware_fingerprint")

        host_key_fingerprint = d.pop("host_key_fingerprint")

        id = d.pop("id")

        node_id = d.pop("node_id")

        state = d.pop("state")

        def _parse_certificate_fingerprint(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        certificate_fingerprint = _parse_certificate_fingerprint(d.pop("certificate_fingerprint", UNSET))


        def _parse_certificate_serial(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        certificate_serial = _parse_certificate_serial(d.pop("certificate_serial", UNSET))


        def _parse_decided_at(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        decided_at = _parse_decided_at(d.pop("decided_at", UNSET))


        def _parse_decision_actor(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        decision_actor = _parse_decision_actor(d.pop("decision_actor", UNSET))


        enrollment_summary = cls(
            agent_digest=agent_digest,
            boot_id=boot_id,
            created_at=created_at,
            csr_public_key_fingerprint=csr_public_key_fingerprint,
            hardware_fingerprint=hardware_fingerprint,
            host_key_fingerprint=host_key_fingerprint,
            id=id,
            node_id=node_id,
            state=state,
            certificate_fingerprint=certificate_fingerprint,
            certificate_serial=certificate_serial,
            decided_at=decided_at,
            decision_actor=decision_actor,
        )

        return enrollment_summary
