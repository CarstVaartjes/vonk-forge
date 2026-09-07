from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.rank_provenance_identity_agreement import check_rank_provenance_identity_agreement
from ..models.rank_provenance_identity_agreement import RankProvenanceIdentityAgreement
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.evidence_age import EvidenceAge





T = TypeVar("T", bound="RankProvenance")



@_attrs_define
class RankProvenance:
    """
        Attributes:
            identity_agreement (RankProvenanceIdentityAgreement):
            installation_evidence (EvidenceAge):
            installation_state (str):
            node_id (str):
            rank (int):
            role (str):
            runtime_evidence (EvidenceAge):
            runtime_state (str):
            installation_evidence_sha256 (Union[None, Unset, str]):
            observation_receipt_sha256 (Union[None, Unset, str]):
            observed_artifact_set_sha256 (Union[None, Unset, str]):
            observed_image_digest (Union[None, Unset, str]):
            observed_recipe_sha256 (Union[None, Unset, str]):
            observed_run_generation (Union[None, Unset, int]):
     """

    identity_agreement: RankProvenanceIdentityAgreement
    installation_evidence: 'EvidenceAge'
    installation_state: str
    node_id: str
    rank: int
    role: str
    runtime_evidence: 'EvidenceAge'
    runtime_state: str
    installation_evidence_sha256: Union[None, Unset, str] = UNSET
    observation_receipt_sha256: Union[None, Unset, str] = UNSET
    observed_artifact_set_sha256: Union[None, Unset, str] = UNSET
    observed_image_digest: Union[None, Unset, str] = UNSET
    observed_recipe_sha256: Union[None, Unset, str] = UNSET
    observed_run_generation: Union[None, Unset, int] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.evidence_age import EvidenceAge
        identity_agreement: str = self.identity_agreement

        installation_evidence = self.installation_evidence.to_dict()

        installation_state = self.installation_state

        node_id = self.node_id

        rank = self.rank

        role = self.role

        runtime_evidence = self.runtime_evidence.to_dict()

        runtime_state = self.runtime_state

        installation_evidence_sha256: Union[None, Unset, str]
        if isinstance(self.installation_evidence_sha256, Unset):
            installation_evidence_sha256 = UNSET
        else:
            installation_evidence_sha256 = self.installation_evidence_sha256

        observation_receipt_sha256: Union[None, Unset, str]
        if isinstance(self.observation_receipt_sha256, Unset):
            observation_receipt_sha256 = UNSET
        else:
            observation_receipt_sha256 = self.observation_receipt_sha256

        observed_artifact_set_sha256: Union[None, Unset, str]
        if isinstance(self.observed_artifact_set_sha256, Unset):
            observed_artifact_set_sha256 = UNSET
        else:
            observed_artifact_set_sha256 = self.observed_artifact_set_sha256

        observed_image_digest: Union[None, Unset, str]
        if isinstance(self.observed_image_digest, Unset):
            observed_image_digest = UNSET
        else:
            observed_image_digest = self.observed_image_digest

        observed_recipe_sha256: Union[None, Unset, str]
        if isinstance(self.observed_recipe_sha256, Unset):
            observed_recipe_sha256 = UNSET
        else:
            observed_recipe_sha256 = self.observed_recipe_sha256

        observed_run_generation: Union[None, Unset, int]
        if isinstance(self.observed_run_generation, Unset):
            observed_run_generation = UNSET
        else:
            observed_run_generation = self.observed_run_generation


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "identity_agreement": identity_agreement,
            "installation_evidence": installation_evidence,
            "installation_state": installation_state,
            "node_id": node_id,
            "rank": rank,
            "role": role,
            "runtime_evidence": runtime_evidence,
            "runtime_state": runtime_state,
        })
        if installation_evidence_sha256 is not UNSET:
            field_dict["installation_evidence_sha256"] = installation_evidence_sha256
        if observation_receipt_sha256 is not UNSET:
            field_dict["observation_receipt_sha256"] = observation_receipt_sha256
        if observed_artifact_set_sha256 is not UNSET:
            field_dict["observed_artifact_set_sha256"] = observed_artifact_set_sha256
        if observed_image_digest is not UNSET:
            field_dict["observed_image_digest"] = observed_image_digest
        if observed_recipe_sha256 is not UNSET:
            field_dict["observed_recipe_sha256"] = observed_recipe_sha256
        if observed_run_generation is not UNSET:
            field_dict["observed_run_generation"] = observed_run_generation

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.evidence_age import EvidenceAge
        d = dict(src_dict)
        identity_agreement = check_rank_provenance_identity_agreement(d.pop("identity_agreement"))




        installation_evidence = EvidenceAge.from_dict(d.pop("installation_evidence"))




        installation_state = d.pop("installation_state")

        node_id = d.pop("node_id")

        rank = d.pop("rank")

        role = d.pop("role")

        runtime_evidence = EvidenceAge.from_dict(d.pop("runtime_evidence"))




        runtime_state = d.pop("runtime_state")

        def _parse_installation_evidence_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        installation_evidence_sha256 = _parse_installation_evidence_sha256(d.pop("installation_evidence_sha256", UNSET))


        def _parse_observation_receipt_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        observation_receipt_sha256 = _parse_observation_receipt_sha256(d.pop("observation_receipt_sha256", UNSET))


        def _parse_observed_artifact_set_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        observed_artifact_set_sha256 = _parse_observed_artifact_set_sha256(d.pop("observed_artifact_set_sha256", UNSET))


        def _parse_observed_image_digest(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        observed_image_digest = _parse_observed_image_digest(d.pop("observed_image_digest", UNSET))


        def _parse_observed_recipe_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        observed_recipe_sha256 = _parse_observed_recipe_sha256(d.pop("observed_recipe_sha256", UNSET))


        def _parse_observed_run_generation(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        observed_run_generation = _parse_observed_run_generation(d.pop("observed_run_generation", UNSET))


        rank_provenance = cls(
            identity_agreement=identity_agreement,
            installation_evidence=installation_evidence,
            installation_state=installation_state,
            node_id=node_id,
            rank=rank,
            role=role,
            runtime_evidence=runtime_evidence,
            runtime_state=runtime_state,
            installation_evidence_sha256=installation_evidence_sha256,
            observation_receipt_sha256=observation_receipt_sha256,
            observed_artifact_set_sha256=observed_artifact_set_sha256,
            observed_image_digest=observed_image_digest,
            observed_recipe_sha256=observed_recipe_sha256,
            observed_run_generation=observed_run_generation,
        )

        return rank_provenance
