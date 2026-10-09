from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.certificate_issuance_purpose import CertificateIssuancePurpose
from ..models.certificate_issuance_purpose import check_certificate_issuance_purpose
from typing import cast






T = TypeVar("T", bound="CertificateIssuanceBinding")



@_attrs_define
class CertificateIssuanceBinding:
    """
        Attributes:
            csr_sha256 (str):
            generation (int):
            issuer_fingerprint (str):
            node_id (str):
            not_after (str):
            not_before (str):
            policy_sha256 (str):
            provisioner_kid (str):
            provisioner_name (str):
            purpose (CertificateIssuancePurpose):
            request_id (str):
            serial (str):
            source_serial (None | str):
     """

    csr_sha256: str
    generation: int
    issuer_fingerprint: str
    node_id: str
    not_after: str
    not_before: str
    policy_sha256: str
    provisioner_kid: str
    provisioner_name: str
    purpose: CertificateIssuancePurpose
    request_id: str
    serial: str
    source_serial: None | str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        csr_sha256 = self.csr_sha256

        generation = self.generation

        issuer_fingerprint = self.issuer_fingerprint

        node_id = self.node_id

        not_after = self.not_after

        not_before = self.not_before

        policy_sha256 = self.policy_sha256

        provisioner_kid = self.provisioner_kid

        provisioner_name = self.provisioner_name

        purpose: str = self.purpose

        request_id = self.request_id

        serial = self.serial

        source_serial: None | str
        source_serial = self.source_serial


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "csr_sha256": csr_sha256,
            "generation": generation,
            "issuer_fingerprint": issuer_fingerprint,
            "node_id": node_id,
            "not_after": not_after,
            "not_before": not_before,
            "policy_sha256": policy_sha256,
            "provisioner_kid": provisioner_kid,
            "provisioner_name": provisioner_name,
            "purpose": purpose,
            "request_id": request_id,
            "serial": serial,
            "source_serial": source_serial,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        csr_sha256 = d.pop("csr_sha256")

        generation = d.pop("generation")

        issuer_fingerprint = d.pop("issuer_fingerprint")

        node_id = d.pop("node_id")

        not_after = d.pop("not_after")

        not_before = d.pop("not_before")

        policy_sha256 = d.pop("policy_sha256")

        provisioner_kid = d.pop("provisioner_kid")

        provisioner_name = d.pop("provisioner_name")

        purpose = check_certificate_issuance_purpose(d.pop("purpose"))




        request_id = d.pop("request_id")

        serial = d.pop("serial")

        def _parse_source_serial(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        source_serial = _parse_source_serial(d.pop("source_serial"))


        certificate_issuance_binding = cls(
            csr_sha256=csr_sha256,
            generation=generation,
            issuer_fingerprint=issuer_fingerprint,
            node_id=node_id,
            not_after=not_after,
            not_before=not_before,
            policy_sha256=policy_sha256,
            provisioner_kid=provisioner_kid,
            provisioner_name=provisioner_name,
            purpose=purpose,
            request_id=request_id,
            serial=serial,
            source_serial=source_serial,
        )


        certificate_issuance_binding.additional_properties = d
        return certificate_issuance_binding

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
