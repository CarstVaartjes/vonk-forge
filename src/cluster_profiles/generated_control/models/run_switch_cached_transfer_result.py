from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import cast, Union
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.run_switch_cached_transfer_result_cached_target_totals import RunSwitchCachedTransferResultCachedTargetTotals





T = TypeVar("T", bound="RunSwitchCachedTransferResult")



@_attrs_define
class RunSwitchCachedTransferResult:
    """
        Attributes:
            cached_nodes (list[str]):
            cached_target_totals (RunSwitchCachedTransferResultCachedTargetTotals):
            phase (Literal['transfer']):
            skipped (bool):
            subphase (Literal['target-copy']):
            verified (bool):
            verified_build_id (Union[None, str]):
            verified_digests (list[str]):
            verified_image_digest (str):
            verified_oci_layout_sha256 (str):
     """

    cached_nodes: list[str]
    cached_target_totals: 'RunSwitchCachedTransferResultCachedTargetTotals'
    phase: Literal['transfer']
    skipped: bool
    subphase: Literal['target-copy']
    verified: bool
    verified_build_id: Union[None, str]
    verified_digests: list[str]
    verified_image_digest: str
    verified_oci_layout_sha256: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.run_switch_cached_transfer_result_cached_target_totals import RunSwitchCachedTransferResultCachedTargetTotals
        cached_nodes = self.cached_nodes



        cached_target_totals = self.cached_target_totals.to_dict()

        phase = self.phase

        skipped = self.skipped

        subphase = self.subphase

        verified = self.verified

        verified_build_id: Union[None, str]
        verified_build_id = self.verified_build_id

        verified_digests = self.verified_digests



        verified_image_digest = self.verified_image_digest

        verified_oci_layout_sha256 = self.verified_oci_layout_sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "cached_nodes": cached_nodes,
            "cached_target_totals": cached_target_totals,
            "phase": phase,
            "skipped": skipped,
            "subphase": subphase,
            "verified": verified,
            "verified_build_id": verified_build_id,
            "verified_digests": verified_digests,
            "verified_image_digest": verified_image_digest,
            "verified_oci_layout_sha256": verified_oci_layout_sha256,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.run_switch_cached_transfer_result_cached_target_totals import RunSwitchCachedTransferResultCachedTargetTotals
        d = dict(src_dict)
        cached_nodes = cast(list[str], d.pop("cached_nodes"))


        cached_target_totals = RunSwitchCachedTransferResultCachedTargetTotals.from_dict(d.pop("cached_target_totals"))




        phase = cast(Literal['transfer'] , d.pop("phase"))
        if phase != 'transfer':
            raise ValueError(f"phase must match const 'transfer', got '{phase}'")

        skipped = d.pop("skipped")

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

        run_switch_cached_transfer_result = cls(
            cached_nodes=cached_nodes,
            cached_target_totals=cached_target_totals,
            phase=phase,
            skipped=skipped,
            subphase=subphase,
            verified=verified,
            verified_build_id=verified_build_id,
            verified_digests=verified_digests,
            verified_image_digest=verified_image_digest,
            verified_oci_layout_sha256=verified_oci_layout_sha256,
        )

        return run_switch_cached_transfer_result
