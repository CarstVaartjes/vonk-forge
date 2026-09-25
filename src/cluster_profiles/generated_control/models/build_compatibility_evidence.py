from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.build_compatibility_evidence_state import BuildCompatibilityEvidenceState
from ..models.build_compatibility_evidence_state import check_build_compatibility_evidence_state
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="BuildCompatibilityEvidence")



@_attrs_define
class BuildCompatibilityEvidence:
    """
        Attributes:
            expected_architecture (str):
            state (BuildCompatibilityEvidenceState):
            detail (Union[None, Unset, str]):
            evidence_digest (Union[None, Unset, str]):
            observed_architecture (Union[None, Unset, str]):
     """

    expected_architecture: str
    state: BuildCompatibilityEvidenceState
    detail: Union[None, Unset, str] = UNSET
    evidence_digest: Union[None, Unset, str] = UNSET
    observed_architecture: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        expected_architecture = self.expected_architecture

        state: str = self.state

        detail: Union[None, Unset, str]
        if isinstance(self.detail, Unset):
            detail = UNSET
        else:
            detail = self.detail

        evidence_digest: Union[None, Unset, str]
        if isinstance(self.evidence_digest, Unset):
            evidence_digest = UNSET
        else:
            evidence_digest = self.evidence_digest

        observed_architecture: Union[None, Unset, str]
        if isinstance(self.observed_architecture, Unset):
            observed_architecture = UNSET
        else:
            observed_architecture = self.observed_architecture


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "expected_architecture": expected_architecture,
            "state": state,
        })
        if detail is not UNSET:
            field_dict["detail"] = detail
        if evidence_digest is not UNSET:
            field_dict["evidence_digest"] = evidence_digest
        if observed_architecture is not UNSET:
            field_dict["observed_architecture"] = observed_architecture

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        expected_architecture = d.pop("expected_architecture")

        state = check_build_compatibility_evidence_state(d.pop("state"))




        def _parse_detail(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        detail = _parse_detail(d.pop("detail", UNSET))


        def _parse_evidence_digest(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        evidence_digest = _parse_evidence_digest(d.pop("evidence_digest", UNSET))


        def _parse_observed_architecture(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        observed_architecture = _parse_observed_architecture(d.pop("observed_architecture", UNSET))


        build_compatibility_evidence = cls(
            expected_architecture=expected_architecture,
            state=state,
            detail=detail,
            evidence_digest=evidence_digest,
            observed_architecture=observed_architecture,
        )

        return build_compatibility_evidence
