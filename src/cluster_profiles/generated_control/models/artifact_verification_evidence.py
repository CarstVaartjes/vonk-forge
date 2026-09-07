from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="ArtifactVerificationEvidence")



@_attrs_define
class ArtifactVerificationEvidence:
    """ One node's immutable artifact handoff evidence.

        Attributes:
            node_id (str):
            copied_bytes (Union[None, Unset, int]):
            downloaded_bytes (Union[None, Unset, int]):
            error (Union[None, Unset, str]):
            imported_image_digest (Union[None, Unset, str]):
            reason (Union[None, Unset, str]):
            uncertain (Union[Unset, bool]):  Default: False.
            verified (Union[None, Unset, bool]):
            verified_digests (Union[Unset, list[str]]):
            verified_image_digest (Union[None, Unset, str]):
            verified_oci_layout_sha256 (Union[None, Unset, str]):
     """

    node_id: str
    copied_bytes: Union[None, Unset, int] = UNSET
    downloaded_bytes: Union[None, Unset, int] = UNSET
    error: Union[None, Unset, str] = UNSET
    imported_image_digest: Union[None, Unset, str] = UNSET
    reason: Union[None, Unset, str] = UNSET
    uncertain: Union[Unset, bool] = False
    verified: Union[None, Unset, bool] = UNSET
    verified_digests: Union[Unset, list[str]] = UNSET
    verified_image_digest: Union[None, Unset, str] = UNSET
    verified_oci_layout_sha256: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        node_id = self.node_id

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

        error: Union[None, Unset, str]
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        imported_image_digest: Union[None, Unset, str]
        if isinstance(self.imported_image_digest, Unset):
            imported_image_digest = UNSET
        else:
            imported_image_digest = self.imported_image_digest

        reason: Union[None, Unset, str]
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        uncertain = self.uncertain

        verified: Union[None, Unset, bool]
        if isinstance(self.verified, Unset):
            verified = UNSET
        else:
            verified = self.verified

        verified_digests: Union[Unset, list[str]] = UNSET
        if not isinstance(self.verified_digests, Unset):
            verified_digests = self.verified_digests



        verified_image_digest: Union[None, Unset, str]
        if isinstance(self.verified_image_digest, Unset):
            verified_image_digest = UNSET
        else:
            verified_image_digest = self.verified_image_digest

        verified_oci_layout_sha256: Union[None, Unset, str]
        if isinstance(self.verified_oci_layout_sha256, Unset):
            verified_oci_layout_sha256 = UNSET
        else:
            verified_oci_layout_sha256 = self.verified_oci_layout_sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "node_id": node_id,
        })
        if copied_bytes is not UNSET:
            field_dict["copied_bytes"] = copied_bytes
        if downloaded_bytes is not UNSET:
            field_dict["downloaded_bytes"] = downloaded_bytes
        if error is not UNSET:
            field_dict["error"] = error
        if imported_image_digest is not UNSET:
            field_dict["imported_image_digest"] = imported_image_digest
        if reason is not UNSET:
            field_dict["reason"] = reason
        if uncertain is not UNSET:
            field_dict["uncertain"] = uncertain
        if verified is not UNSET:
            field_dict["verified"] = verified
        if verified_digests is not UNSET:
            field_dict["verified_digests"] = verified_digests
        if verified_image_digest is not UNSET:
            field_dict["verified_image_digest"] = verified_image_digest
        if verified_oci_layout_sha256 is not UNSET:
            field_dict["verified_oci_layout_sha256"] = verified_oci_layout_sha256

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        node_id = d.pop("node_id")

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


        def _parse_error(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        error = _parse_error(d.pop("error", UNSET))


        def _parse_imported_image_digest(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        imported_image_digest = _parse_imported_image_digest(d.pop("imported_image_digest", UNSET))


        def _parse_reason(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        reason = _parse_reason(d.pop("reason", UNSET))


        uncertain = d.pop("uncertain", UNSET)

        def _parse_verified(data: object) -> Union[None, Unset, bool]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, bool], data)

        verified = _parse_verified(d.pop("verified", UNSET))


        verified_digests = cast(list[str], d.pop("verified_digests", UNSET))


        def _parse_verified_image_digest(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        verified_image_digest = _parse_verified_image_digest(d.pop("verified_image_digest", UNSET))


        def _parse_verified_oci_layout_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        verified_oci_layout_sha256 = _parse_verified_oci_layout_sha256(d.pop("verified_oci_layout_sha256", UNSET))


        artifact_verification_evidence = cls(
            node_id=node_id,
            copied_bytes=copied_bytes,
            downloaded_bytes=downloaded_bytes,
            error=error,
            imported_image_digest=imported_image_digest,
            reason=reason,
            uncertain=uncertain,
            verified=verified,
            verified_digests=verified_digests,
            verified_image_digest=verified_image_digest,
            verified_oci_layout_sha256=verified_oci_layout_sha256,
        )

        return artifact_verification_evidence
