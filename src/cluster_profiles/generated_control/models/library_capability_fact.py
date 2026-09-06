from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.library_capability_fact_evidence_status import check_library_capability_fact_evidence_status
from ..models.library_capability_fact_evidence_status import LibraryCapabilityFactEvidenceStatus
from ..models.library_capability_fact_support import check_library_capability_fact_support
from ..models.library_capability_fact_support import LibraryCapabilityFactSupport
from typing import cast
from typing import cast, Union

if TYPE_CHECKING:
  from ..models.library_capability_provenance import LibraryCapabilityProvenance





T = TypeVar("T", bound="LibraryCapabilityFact")



@_attrs_define
class LibraryCapabilityFact:
    """ One explicit capability assertion; absence is never represented as support.

        Attributes:
            capability (str):
            evidence_digest (Union[None, str]):
            evidence_status (LibraryCapabilityFactEvidenceStatus):
            provenance (LibraryCapabilityProvenance): Exact source identity and bounded location for one capability
                inventory.
            support (LibraryCapabilityFactSupport):
     """

    capability: str
    evidence_digest: Union[None, str]
    evidence_status: LibraryCapabilityFactEvidenceStatus
    provenance: 'LibraryCapabilityProvenance'
    support: LibraryCapabilityFactSupport





    def to_dict(self) -> dict[str, Any]:
        from ..models.library_capability_provenance import LibraryCapabilityProvenance
        capability = self.capability

        evidence_digest: Union[None, str]
        evidence_digest = self.evidence_digest

        evidence_status: str = self.evidence_status

        provenance = self.provenance.to_dict()

        support: str = self.support


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "capability": capability,
            "evidence_digest": evidence_digest,
            "evidence_status": evidence_status,
            "provenance": provenance,
            "support": support,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.library_capability_provenance import LibraryCapabilityProvenance
        d = dict(src_dict)
        capability = d.pop("capability")

        def _parse_evidence_digest(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        evidence_digest = _parse_evidence_digest(d.pop("evidence_digest"))


        evidence_status = check_library_capability_fact_evidence_status(d.pop("evidence_status"))




        provenance = LibraryCapabilityProvenance.from_dict(d.pop("provenance"))




        support = check_library_capability_fact_support(d.pop("support"))




        library_capability_fact = cls(
            capability=capability,
            evidence_digest=evidence_digest,
            evidence_status=evidence_status,
            provenance=provenance,
            support=support,
        )

        return library_capability_fact
