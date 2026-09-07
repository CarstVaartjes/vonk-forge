from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="ModelCapabilityProvenance")



@_attrs_define
class ModelCapabilityProvenance:
    """
        Attributes:
            evidence_digest (str):
            source_revision (str):
            source_url (str):
     """

    evidence_digest: str
    source_revision: str
    source_url: str





    def to_dict(self) -> dict[str, Any]:
        evidence_digest = self.evidence_digest

        source_revision = self.source_revision

        source_url = self.source_url


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "evidence_digest": evidence_digest,
            "source_revision": source_revision,
            "source_url": source_url,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        evidence_digest = d.pop("evidence_digest")

        source_revision = d.pop("source_revision")

        source_url = d.pop("source_url")

        model_capability_provenance = cls(
            evidence_digest=evidence_digest,
            source_revision=source_revision,
            source_url=source_url,
        )

        return model_capability_provenance
