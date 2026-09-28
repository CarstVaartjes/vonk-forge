from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="OperationEvidenceProvenance")



@_attrs_define
class OperationEvidenceProvenance:
    """
        Attributes:
            source (str):
            authority_revision (None | str | Unset):
            collected_at (None | str | Unset):
            evidence_digest (None | str | Unset):
     """

    source: str
    authority_revision: None | str | Unset = UNSET
    collected_at: None | str | Unset = UNSET
    evidence_digest: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        source = self.source

        authority_revision: None | str | Unset
        if isinstance(self.authority_revision, Unset):
            authority_revision = UNSET
        else:
            authority_revision = self.authority_revision

        collected_at: None | str | Unset
        if isinstance(self.collected_at, Unset):
            collected_at = UNSET
        else:
            collected_at = self.collected_at

        evidence_digest: None | str | Unset
        if isinstance(self.evidence_digest, Unset):
            evidence_digest = UNSET
        else:
            evidence_digest = self.evidence_digest


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "source": source,
        })
        if authority_revision is not UNSET:
            field_dict["authority_revision"] = authority_revision
        if collected_at is not UNSET:
            field_dict["collected_at"] = collected_at
        if evidence_digest is not UNSET:
            field_dict["evidence_digest"] = evidence_digest

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        source = d.pop("source")

        def _parse_authority_revision(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        authority_revision = _parse_authority_revision(d.pop("authority_revision", UNSET))


        def _parse_collected_at(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        collected_at = _parse_collected_at(d.pop("collected_at", UNSET))


        def _parse_evidence_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        evidence_digest = _parse_evidence_digest(d.pop("evidence_digest", UNSET))


        operation_evidence_provenance = cls(
            source=source,
            authority_revision=authority_revision,
            collected_at=collected_at,
            evidence_digest=evidence_digest,
        )

        return operation_evidence_provenance
