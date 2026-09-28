from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.capability_evidence_evidence import CapabilityEvidenceEvidence
from ..models.capability_evidence_evidence import check_capability_evidence_evidence
from ..models.capability_evidence_support import CapabilityEvidenceSupport
from ..models.capability_evidence_support import check_capability_evidence_support
from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="CapabilityEvidence")



@_attrs_define
class CapabilityEvidence:
    """ One capability's declaration and evidence, kept separate by owner.

        Attributes:
            declared (bool | None):
            evidence (CapabilityEvidenceEvidence):
            name (str):
            support (CapabilityEvidenceSupport):
            detail (None | str | Unset):
            evidence_digest (None | str | Unset):
     """

    declared: bool | None
    evidence: CapabilityEvidenceEvidence
    name: str
    support: CapabilityEvidenceSupport
    detail: None | str | Unset = UNSET
    evidence_digest: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        declared: bool | None
        declared = self.declared

        evidence: str = self.evidence

        name = self.name

        support: str = self.support

        detail: None | str | Unset
        if isinstance(self.detail, Unset):
            detail = UNSET
        else:
            detail = self.detail

        evidence_digest: None | str | Unset
        if isinstance(self.evidence_digest, Unset):
            evidence_digest = UNSET
        else:
            evidence_digest = self.evidence_digest


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "declared": declared,
            "evidence": evidence,
            "name": name,
            "support": support,
        })
        if detail is not UNSET:
            field_dict["detail"] = detail
        if evidence_digest is not UNSET:
            field_dict["evidence_digest"] = evidence_digest

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        def _parse_declared(data: object) -> bool | None:
            if data is None:
                return data
            return cast(bool | None, data)

        declared = _parse_declared(d.pop("declared"))


        evidence = check_capability_evidence_evidence(d.pop("evidence"))




        name = d.pop("name")

        support = check_capability_evidence_support(d.pop("support"))




        def _parse_detail(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        detail = _parse_detail(d.pop("detail", UNSET))


        def _parse_evidence_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        evidence_digest = _parse_evidence_digest(d.pop("evidence_digest", UNSET))


        capability_evidence = cls(
            declared=declared,
            evidence=evidence,
            name=name,
            support=support,
            detail=detail,
            evidence_digest=evidence_digest,
        )

        return capability_evidence
