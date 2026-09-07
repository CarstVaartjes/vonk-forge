from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="ModelProvenance")



@_attrs_define
class ModelProvenance:
    """
        Attributes:
            attribution (list[str]):
            evidence_digest (str):
            source_revision (str):
            source_url (str):
     """

    attribution: list[str]
    evidence_digest: str
    source_revision: str
    source_url: str





    def to_dict(self) -> dict[str, Any]:
        attribution = self.attribution



        evidence_digest = self.evidence_digest

        source_revision = self.source_revision

        source_url = self.source_url


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "attribution": attribution,
            "evidence_digest": evidence_digest,
            "source_revision": source_revision,
            "source_url": source_url,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        attribution = cast(list[str], d.pop("attribution"))


        evidence_digest = d.pop("evidence_digest")

        source_revision = d.pop("source_revision")

        source_url = d.pop("source_url")

        model_provenance = cls(
            attribution=attribution,
            evidence_digest=evidence_digest,
            source_revision=source_revision,
            source_url=source_url,
        )

        return model_provenance
