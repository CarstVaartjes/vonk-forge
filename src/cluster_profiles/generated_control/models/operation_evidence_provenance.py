from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="OperationEvidenceProvenance")



@_attrs_define
class OperationEvidenceProvenance:
    """
        Attributes:
            source (str):
            authority_revision (Union[None, Unset, str]):
            collected_at (Union[None, Unset, str]):
            evidence_digest (Union[None, Unset, str]):
     """

    source: str
    authority_revision: Union[None, Unset, str] = UNSET
    collected_at: Union[None, Unset, str] = UNSET
    evidence_digest: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        source = self.source

        authority_revision: Union[None, Unset, str]
        if isinstance(self.authority_revision, Unset):
            authority_revision = UNSET
        else:
            authority_revision = self.authority_revision

        collected_at: Union[None, Unset, str]
        if isinstance(self.collected_at, Unset):
            collected_at = UNSET
        else:
            collected_at = self.collected_at

        evidence_digest: Union[None, Unset, str]
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

        def _parse_authority_revision(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        authority_revision = _parse_authority_revision(d.pop("authority_revision", UNSET))


        def _parse_collected_at(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        collected_at = _parse_collected_at(d.pop("collected_at", UNSET))


        def _parse_evidence_digest(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        evidence_digest = _parse_evidence_digest(d.pop("evidence_digest", UNSET))


        operation_evidence_provenance = cls(
            source=source,
            authority_revision=authority_revision,
            collected_at=collected_at,
            evidence_digest=evidence_digest,
        )

        return operation_evidence_provenance
