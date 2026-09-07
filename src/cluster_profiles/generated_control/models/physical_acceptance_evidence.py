from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.physical_acceptance_evidence_state import check_physical_acceptance_evidence_state
from ..models.physical_acceptance_evidence_state import PhysicalAcceptanceEvidenceState
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.evidence_age import EvidenceAge





T = TypeVar("T", bound="PhysicalAcceptanceEvidence")



@_attrs_define
class PhysicalAcceptanceEvidence:
    """
        Attributes:
            evidence (EvidenceAge):
            state (PhysicalAcceptanceEvidenceState):
            boundary (Union[Literal['physical_acceptance'], Unset]):  Default: 'physical_acceptance'.
            evidence_sha256 (Union[None, Unset, str]):
     """

    evidence: 'EvidenceAge'
    state: PhysicalAcceptanceEvidenceState
    boundary: Union[Literal['physical_acceptance'], Unset] = 'physical_acceptance'
    evidence_sha256: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.evidence_age import EvidenceAge
        evidence = self.evidence.to_dict()

        state: str = self.state

        boundary = self.boundary

        evidence_sha256: Union[None, Unset, str]
        if isinstance(self.evidence_sha256, Unset):
            evidence_sha256 = UNSET
        else:
            evidence_sha256 = self.evidence_sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "evidence": evidence,
            "state": state,
        })
        if boundary is not UNSET:
            field_dict["boundary"] = boundary
        if evidence_sha256 is not UNSET:
            field_dict["evidence_sha256"] = evidence_sha256

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.evidence_age import EvidenceAge
        d = dict(src_dict)
        evidence = EvidenceAge.from_dict(d.pop("evidence"))




        state = check_physical_acceptance_evidence_state(d.pop("state"))




        boundary = cast(Union[Literal['physical_acceptance'], Unset] , d.pop("boundary", UNSET))
        if boundary != 'physical_acceptance' and not isinstance(boundary, Unset):
            raise ValueError(f"boundary must match const 'physical_acceptance', got '{boundary}'")

        def _parse_evidence_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        evidence_sha256 = _parse_evidence_sha256(d.pop("evidence_sha256", UNSET))


        physical_acceptance_evidence = cls(
            evidence=evidence,
            state=state,
            boundary=boundary,
            evidence_sha256=evidence_sha256,
        )

        return physical_acceptance_evidence
