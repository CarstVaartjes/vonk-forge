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
            reason (None | str | Unset):
            uncertain (bool | Unset):  Default: False.
     """

    node_id: str
    copied_bytes: int | None | Unset = UNSET
    downloaded_bytes: int | None | Unset = UNSET
    error: None | str | Unset = UNSET
    reason: None | str | Unset = UNSET
    uncertain: bool | Unset = False





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

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        uncertain = self.uncertain


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
        if reason is not UNSET:
            field_dict["reason"] = reason
        if uncertain is not UNSET:
            field_dict["uncertain"] = uncertain

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


        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))


        uncertain = d.pop("uncertain", UNSET)

        artifact_verification_evidence = cls(
            node_id=node_id,
            copied_bytes=copied_bytes,
            downloaded_bytes=downloaded_bytes,
            error=error,
            reason=reason,
            uncertain=uncertain,
        )

        return artifact_verification_evidence
