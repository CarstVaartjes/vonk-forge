from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="ArtifactVerificationEvidence")



@_attrs_define
class ArtifactVerificationEvidence:
    """ One node's immutable artifact handoff evidence.

        Attributes:
            node_id (str):
            copied_bytes (int | None | Unset):
            downloaded_bytes (int | None | Unset):
            error (None | str | Unset):
            imported_image_digest (None | str | Unset):
            reason (None | str | Unset):
            uncertain (bool | Unset):  Default: False.
            verified (bool | None | Unset):
            verified_digests (list[str] | Unset):
            verified_image_digest (None | str | Unset):
            verified_oci_layout_sha256 (None | str | Unset):
     """

    node_id: str
    copied_bytes: int | None | Unset = UNSET
    downloaded_bytes: int | None | Unset = UNSET
    error: None | str | Unset = UNSET
    imported_image_digest: None | str | Unset = UNSET
    reason: None | str | Unset = UNSET
    uncertain: bool | Unset = False
    verified: bool | None | Unset = UNSET
    verified_digests: list[str] | Unset = UNSET
    verified_image_digest: None | str | Unset = UNSET
    verified_oci_layout_sha256: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        node_id = self.node_id

        copied_bytes: int | None | Unset
        if isinstance(self.copied_bytes, Unset):
            copied_bytes = UNSET
        else:
            copied_bytes = self.copied_bytes

        downloaded_bytes: int | None | Unset
        if isinstance(self.downloaded_bytes, Unset):
            downloaded_bytes = UNSET
        else:
            downloaded_bytes = self.downloaded_bytes

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        imported_image_digest: None | str | Unset
        if isinstance(self.imported_image_digest, Unset):
            imported_image_digest = UNSET
        else:
            imported_image_digest = self.imported_image_digest

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        uncertain = self.uncertain

        verified: bool | None | Unset
        if isinstance(self.verified, Unset):
            verified = UNSET
        else:
            verified = self.verified

        verified_digests: list[str] | Unset = UNSET
        if not isinstance(self.verified_digests, Unset):
            verified_digests = self.verified_digests



        verified_image_digest: None | str | Unset
        if isinstance(self.verified_image_digest, Unset):
            verified_image_digest = UNSET
        else:
            verified_image_digest = self.verified_image_digest

        verified_oci_layout_sha256: None | str | Unset
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

        def _parse_copied_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        copied_bytes = _parse_copied_bytes(d.pop("copied_bytes", UNSET))


        def _parse_downloaded_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        downloaded_bytes = _parse_downloaded_bytes(d.pop("downloaded_bytes", UNSET))


        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))


        def _parse_imported_image_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        imported_image_digest = _parse_imported_image_digest(d.pop("imported_image_digest", UNSET))


        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))


        uncertain = d.pop("uncertain", UNSET)

        def _parse_verified(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        verified = _parse_verified(d.pop("verified", UNSET))


        verified_digests = cast(list[str], d.pop("verified_digests", UNSET))


        def _parse_verified_image_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        verified_image_digest = _parse_verified_image_digest(d.pop("verified_image_digest", UNSET))


        def _parse_verified_oci_layout_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

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
