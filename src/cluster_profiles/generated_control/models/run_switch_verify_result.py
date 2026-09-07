from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.run_switch_verify_result_cached_target_totals import RunSwitchVerifyResultCachedTargetTotals
  from ..models.artifact_verification_evidence import ArtifactVerificationEvidence





T = TypeVar("T", bound="RunSwitchVerifyResult")



@_attrs_define
class RunSwitchVerifyResult:
    """
        Attributes:
            phase (Literal['verify']):
            subphase (Literal['target-copy']):
            verified (bool):
            verified_build_id (Union[None, str]):
            verified_digests (list[str]):
            verified_image_digest (str):
            verified_oci_layout_sha256 (str):
            cached_nodes (Union[Unset, list[str]]):
            cached_target_totals (Union[Unset, RunSwitchVerifyResultCachedTargetTotals]):
            evidence (Union[Unset, list['ArtifactVerificationEvidence']]):
            skipped (Union[Unset, bool]):  Default: False.
            verified_registry_manifest_digest (Union[None, Unset, str]):
     """

    phase: Literal['verify']
    subphase: Literal['target-copy']
    verified: bool
    verified_build_id: Union[None, str]
    verified_digests: list[str]
    verified_image_digest: str
    verified_oci_layout_sha256: str
    cached_nodes: Union[Unset, list[str]] = UNSET
    cached_target_totals: Union[Unset, 'RunSwitchVerifyResultCachedTargetTotals'] = UNSET
    evidence: Union[Unset, list['ArtifactVerificationEvidence']] = UNSET
    skipped: Union[Unset, bool] = False
    verified_registry_manifest_digest: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.run_switch_verify_result_cached_target_totals import RunSwitchVerifyResultCachedTargetTotals
        from ..models.artifact_verification_evidence import ArtifactVerificationEvidence
        phase = self.phase

        subphase = self.subphase

        verified = self.verified

        verified_build_id: Union[None, str]
        verified_build_id = self.verified_build_id

        verified_digests = self.verified_digests



        verified_image_digest = self.verified_image_digest

        verified_oci_layout_sha256 = self.verified_oci_layout_sha256

        cached_nodes: Union[Unset, list[str]] = UNSET
        if not isinstance(self.cached_nodes, Unset):
            cached_nodes = self.cached_nodes



        cached_target_totals: Union[Unset, dict[str, Any]] = UNSET
        if not isinstance(self.cached_target_totals, Unset):
            cached_target_totals = self.cached_target_totals.to_dict()

        evidence: Union[Unset, list[dict[str, Any]]] = UNSET
        if not isinstance(self.evidence, Unset):
            evidence = []
            for evidence_item_data in self.evidence:
                evidence_item = evidence_item_data.to_dict()
                evidence.append(evidence_item)



        skipped = self.skipped

        verified_registry_manifest_digest: Union[None, Unset, str]
        if isinstance(self.verified_registry_manifest_digest, Unset):
            verified_registry_manifest_digest = UNSET
        else:
            verified_registry_manifest_digest = self.verified_registry_manifest_digest


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "phase": phase,
            "subphase": subphase,
            "verified": verified,
            "verified_build_id": verified_build_id,
            "verified_digests": verified_digests,
            "verified_image_digest": verified_image_digest,
            "verified_oci_layout_sha256": verified_oci_layout_sha256,
        })
        if cached_nodes is not UNSET:
            field_dict["cached_nodes"] = cached_nodes
        if cached_target_totals is not UNSET:
            field_dict["cached_target_totals"] = cached_target_totals
        if evidence is not UNSET:
            field_dict["evidence"] = evidence
        if skipped is not UNSET:
            field_dict["skipped"] = skipped
        if verified_registry_manifest_digest is not UNSET:
            field_dict["verified_registry_manifest_digest"] = verified_registry_manifest_digest

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.run_switch_verify_result_cached_target_totals import RunSwitchVerifyResultCachedTargetTotals
        from ..models.artifact_verification_evidence import ArtifactVerificationEvidence
        d = dict(src_dict)
        phase = cast(Literal['verify'] , d.pop("phase"))
        if phase != 'verify':
            raise ValueError(f"phase must match const 'verify', got '{phase}'")

        subphase = cast(Literal['target-copy'] , d.pop("subphase"))
        if subphase != 'target-copy':
            raise ValueError(f"subphase must match const 'target-copy', got '{subphase}'")

        verified = d.pop("verified")

        def _parse_verified_build_id(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        verified_build_id = _parse_verified_build_id(d.pop("verified_build_id"))


        verified_digests = cast(list[str], d.pop("verified_digests"))


        verified_image_digest = d.pop("verified_image_digest")

        verified_oci_layout_sha256 = d.pop("verified_oci_layout_sha256")

        cached_nodes = cast(list[str], d.pop("cached_nodes", UNSET))


        _cached_target_totals = d.pop("cached_target_totals", UNSET)
        cached_target_totals: Union[Unset, RunSwitchVerifyResultCachedTargetTotals]
        if isinstance(_cached_target_totals,  Unset):
            cached_target_totals = UNSET
        else:
            cached_target_totals = RunSwitchVerifyResultCachedTargetTotals.from_dict(_cached_target_totals)




        evidence = []
        _evidence = d.pop("evidence", UNSET)
        for evidence_item_data in (_evidence or []):
            evidence_item = ArtifactVerificationEvidence.from_dict(evidence_item_data)



            evidence.append(evidence_item)


        skipped = d.pop("skipped", UNSET)

        def _parse_verified_registry_manifest_digest(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        verified_registry_manifest_digest = _parse_verified_registry_manifest_digest(d.pop("verified_registry_manifest_digest", UNSET))


        run_switch_verify_result = cls(
            phase=phase,
            subphase=subphase,
            verified=verified,
            verified_build_id=verified_build_id,
            verified_digests=verified_digests,
            verified_image_digest=verified_image_digest,
            verified_oci_layout_sha256=verified_oci_layout_sha256,
            cached_nodes=cached_nodes,
            cached_target_totals=cached_target_totals,
            evidence=evidence,
            skipped=skipped,
            verified_registry_manifest_digest=verified_registry_manifest_digest,
        )

        return run_switch_verify_result
