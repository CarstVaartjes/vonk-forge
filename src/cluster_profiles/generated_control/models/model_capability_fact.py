from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.model_capability_fact_capability import check_model_capability_fact_capability
from ..models.model_capability_fact_capability import ModelCapabilityFactCapability
from ..models.model_capability_fact_evidence_status import check_model_capability_fact_evidence_status
from ..models.model_capability_fact_evidence_status import ModelCapabilityFactEvidenceStatus
from ..models.model_capability_fact_support import check_model_capability_fact_support
from ..models.model_capability_fact_support import ModelCapabilityFactSupport
from typing import cast
from typing import cast, Union






T = TypeVar("T", bound="ModelCapabilityFact")



@_attrs_define
class ModelCapabilityFact:
    """
        Attributes:
            capability (ModelCapabilityFactCapability):
            evidence_digest (Union[None, str]):
            evidence_status (ModelCapabilityFactEvidenceStatus):
            support (ModelCapabilityFactSupport):
     """

    capability: ModelCapabilityFactCapability
    evidence_digest: Union[None, str]
    evidence_status: ModelCapabilityFactEvidenceStatus
    support: ModelCapabilityFactSupport





    def to_dict(self) -> dict[str, Any]:
        capability: str = self.capability

        evidence_digest: Union[None, str]
        evidence_digest = self.evidence_digest

        evidence_status: str = self.evidence_status

        support: str = self.support


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "capability": capability,
            "evidence_digest": evidence_digest,
            "evidence_status": evidence_status,
            "support": support,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        capability = check_model_capability_fact_capability(d.pop("capability"))




        def _parse_evidence_digest(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        evidence_digest = _parse_evidence_digest(d.pop("evidence_digest"))


        evidence_status = check_model_capability_fact_evidence_status(d.pop("evidence_status"))




        support = check_model_capability_fact_support(d.pop("support"))




        model_capability_fact = cls(
            capability=capability,
            evidence_digest=evidence_digest,
            evidence_status=evidence_status,
            support=support,
        )

        return model_capability_fact
