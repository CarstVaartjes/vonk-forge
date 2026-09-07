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






T = TypeVar("T", bound="RunSwitchTargetTransferEvidenceResult")



@_attrs_define
class RunSwitchTargetTransferEvidenceResult:
    """
        Attributes:
            imported_image_digest (str):
            node_id (str):
            phase (Literal['transfer']):
            subphase (Literal['target-copy']):
            verified (bool):
            verified_digests (list[str]):
            verified_image_digest (str):
            verified_oci_layout_sha256 (str):
            copied_bytes (Union[None, Unset, int]):
            downloaded_bytes (Union[None, Unset, int]):
     """

    imported_image_digest: str
    node_id: str
    phase: Literal['transfer']
    subphase: Literal['target-copy']
    verified: bool
    verified_digests: list[str]
    verified_image_digest: str
    verified_oci_layout_sha256: str
    copied_bytes: Union[None, Unset, int] = UNSET
    downloaded_bytes: Union[None, Unset, int] = UNSET





    def to_dict(self) -> dict[str, Any]:
        imported_image_digest = self.imported_image_digest

        node_id = self.node_id

        phase = self.phase

        subphase = self.subphase

        verified = self.verified

        verified_digests = self.verified_digests



        verified_image_digest = self.verified_image_digest

        verified_oci_layout_sha256 = self.verified_oci_layout_sha256

        copied_bytes: Union[None, Unset, int]
        if isinstance(self.copied_bytes, Unset):
            copied_bytes = UNSET
        else:
            copied_bytes = self.copied_bytes

        downloaded_bytes: Union[None, Unset, int]
        if isinstance(self.downloaded_bytes, Unset):
            downloaded_bytes = UNSET
        else:
            downloaded_bytes = self.downloaded_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "imported_image_digest": imported_image_digest,
            "node_id": node_id,
            "phase": phase,
            "subphase": subphase,
            "verified": verified,
            "verified_digests": verified_digests,
            "verified_image_digest": verified_image_digest,
            "verified_oci_layout_sha256": verified_oci_layout_sha256,
        })
        if copied_bytes is not UNSET:
            field_dict["copied_bytes"] = copied_bytes
        if downloaded_bytes is not UNSET:
            field_dict["downloaded_bytes"] = downloaded_bytes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        imported_image_digest = d.pop("imported_image_digest")

        node_id = d.pop("node_id")

        phase = cast(Literal['transfer'] , d.pop("phase"))
        if phase != 'transfer':
            raise ValueError(f"phase must match const 'transfer', got '{phase}'")

        subphase = cast(Literal['target-copy'] , d.pop("subphase"))
        if subphase != 'target-copy':
            raise ValueError(f"subphase must match const 'target-copy', got '{subphase}'")

        verified = d.pop("verified")

        verified_digests = cast(list[str], d.pop("verified_digests"))


        verified_image_digest = d.pop("verified_image_digest")

        verified_oci_layout_sha256 = d.pop("verified_oci_layout_sha256")

        def _parse_copied_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        copied_bytes = _parse_copied_bytes(d.pop("copied_bytes", UNSET))


        def _parse_downloaded_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        downloaded_bytes = _parse_downloaded_bytes(d.pop("downloaded_bytes", UNSET))


        run_switch_target_transfer_evidence_result = cls(
            imported_image_digest=imported_image_digest,
            node_id=node_id,
            phase=phase,
            subphase=subphase,
            verified=verified,
            verified_digests=verified_digests,
            verified_image_digest=verified_image_digest,
            verified_oci_layout_sha256=verified_oci_layout_sha256,
            copied_bytes=copied_bytes,
            downloaded_bytes=downloaded_bytes,
        )

        return run_switch_target_transfer_evidence_result
