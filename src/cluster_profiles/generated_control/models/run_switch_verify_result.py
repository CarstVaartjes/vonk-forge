from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.artifact_verification_evidence import ArtifactVerificationEvidence
  from ..models.run_switch_verify_result_cached_target_totals import RunSwitchVerifyResultCachedTargetTotals





T = TypeVar("T", bound="RunSwitchVerifyResult")



@_attrs_define
class RunSwitchVerifyResult:
    """
        Attributes:
            phase (Literal['verify']):
            subphase (Literal['target-copy']):
            verified (bool):
            verified_build_id (None | str):
            verified_digests (list[str]):
            verified_image_digest (str):
            verified_oci_layout_sha256 (str):
            cached_nodes (list[str] | Unset):
            cached_target_totals (RunSwitchVerifyResultCachedTargetTotals | Unset):
            evidence (list[ArtifactVerificationEvidence] | Unset):
            skipped (bool | Unset):  Default: False.
     """

    phase: Literal['verify']
    subphase: Literal['target-copy']
    verified: bool
    verified_build_id: None | str
    verified_digests: list[str]
    verified_image_digest: str
    verified_oci_layout_sha256: str
    cached_nodes: list[str] | Unset = UNSET
    cached_target_totals: RunSwitchVerifyResultCachedTargetTotals | Unset = UNSET
    evidence: list[ArtifactVerificationEvidence] | Unset = UNSET
    skipped: bool | Unset = False





    def to_dict(self) -> dict[str, Any]:
        from ..models.artifact_verification_evidence import ArtifactVerificationEvidence # noqa: PLC0415
        from ..models.run_switch_verify_result_cached_target_totals import RunSwitchVerifyResultCachedTargetTotals # noqa: PLC0415
        phase = self.phase

        subphase = self.subphase

        verified = self.verified

        verified_build_id: None | str
        verified_build_id = self.verified_build_id

        verified_digests = self.verified_digests



        verified_image_digest = self.verified_image_digest

        verified_oci_layout_sha256 = self.verified_oci_layout_sha256

        cached_nodes: list[str] | Unset = UNSET
        if not isinstance(self.cached_nodes, Unset):
            cached_nodes = self.cached_nodes



        cached_target_totals: dict[str, Any] | Unset = UNSET
        if not isinstance(self.cached_target_totals, Unset):
            cached_target_totals = self.cached_target_totals.to_dict()

        evidence: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.evidence, Unset):
            evidence = []
            for evidence_item_data in self.evidence:
                evidence_item = evidence_item_data.to_dict()
                evidence.append(evidence_item)



        skipped = self.skipped


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

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.artifact_verification_evidence import ArtifactVerificationEvidence # noqa: PLC0415
        from ..models.run_switch_verify_result_cached_target_totals import RunSwitchVerifyResultCachedTargetTotals # noqa: PLC0415
        d = dict(src_dict)
        phase = cast(Literal['verify'] , d.pop("phase"))
        if phase != 'verify':
            raise ValueError(f"phase must match const 'verify', got '{phase}'")

        subphase = cast(Literal['target-copy'] , d.pop("subphase"))
        if subphase != 'target-copy':
            raise ValueError(f"subphase must match const 'target-copy', got '{subphase}'")

        verified = d.pop("verified")

        def _parse_verified_build_id(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        verified_build_id = _parse_verified_build_id(d.pop("verified_build_id"))


        verified_digests = cast(list[str], d.pop("verified_digests"))


        verified_image_digest = d.pop("verified_image_digest")

        verified_oci_layout_sha256 = d.pop("verified_oci_layout_sha256")

        cached_nodes = cast(list[str], d.pop("cached_nodes", UNSET))


        _cached_target_totals = d.pop("cached_target_totals", UNSET)
        cached_target_totals: RunSwitchVerifyResultCachedTargetTotals | Unset
        if isinstance(_cached_target_totals,  Unset):
            cached_target_totals = UNSET
        else:
            cached_target_totals = RunSwitchVerifyResultCachedTargetTotals.from_dict(_cached_target_totals)




        _evidence = d.pop("evidence", UNSET)
        evidence: list[ArtifactVerificationEvidence] | Unset = UNSET
        if _evidence is not UNSET:
            evidence = []
            for evidence_item_data in _evidence:
                evidence_item = ArtifactVerificationEvidence.from_dict(evidence_item_data)



                evidence.append(evidence_item)


        skipped = d.pop("skipped", UNSET)

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
        )

        return run_switch_verify_result
