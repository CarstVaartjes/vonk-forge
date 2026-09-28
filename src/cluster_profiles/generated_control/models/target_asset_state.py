from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.target_asset_state_state import check_target_asset_state_state
from ..models.target_asset_state_state import TargetAssetStateState
from ..types import UNSET, Unset
from typing import cast
import datetime






T = TypeVar("T", bound="TargetAssetState")



@_attrs_define
class TargetAssetState:
    """ Staging and verification state for one immutable asset on one Spark.

        Attributes:
            node_id (str):
            state (TargetAssetStateState):
            expected_bytes (int | None | Unset):
            imported_image_digest (None | str | Unset):
            missing_bytes (int | None | Unset):
            present_bytes (int | Unset):  Default: 0.
            reason (None | str | Unset):
            verified_at (datetime.datetime | None | Unset):
            verified_sha256 (None | str | Unset):
     """

    node_id: str
    state: TargetAssetStateState
    expected_bytes: int | None | Unset = UNSET
    imported_image_digest: None | str | Unset = UNSET
    missing_bytes: int | None | Unset = UNSET
    present_bytes: int | Unset = 0
    reason: None | str | Unset = UNSET
    verified_at: datetime.datetime | None | Unset = UNSET
    verified_sha256: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        node_id = self.node_id

        state: str = self.state

        expected_bytes: int | None | Unset
        if isinstance(self.expected_bytes, Unset):
            expected_bytes = UNSET
        else:
            expected_bytes = self.expected_bytes

        imported_image_digest: None | str | Unset
        if isinstance(self.imported_image_digest, Unset):
            imported_image_digest = UNSET
        else:
            imported_image_digest = self.imported_image_digest

        missing_bytes: int | None | Unset
        if isinstance(self.missing_bytes, Unset):
            missing_bytes = UNSET
        else:
            missing_bytes = self.missing_bytes

        present_bytes = self.present_bytes

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        verified_at: None | str | Unset
        if isinstance(self.verified_at, Unset):
            verified_at = UNSET
        elif isinstance(self.verified_at, datetime.datetime):
            verified_at = self.verified_at.isoformat()
        else:
            verified_at = self.verified_at

        verified_sha256: None | str | Unset
        if isinstance(self.verified_sha256, Unset):
            verified_sha256 = UNSET
        else:
            verified_sha256 = self.verified_sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "node_id": node_id,
            "state": state,
        })
        if expected_bytes is not UNSET:
            field_dict["expected_bytes"] = expected_bytes
        if imported_image_digest is not UNSET:
            field_dict["imported_image_digest"] = imported_image_digest
        if missing_bytes is not UNSET:
            field_dict["missing_bytes"] = missing_bytes
        if present_bytes is not UNSET:
            field_dict["present_bytes"] = present_bytes
        if reason is not UNSET:
            field_dict["reason"] = reason
        if verified_at is not UNSET:
            field_dict["verified_at"] = verified_at
        if verified_sha256 is not UNSET:
            field_dict["verified_sha256"] = verified_sha256

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        node_id = d.pop("node_id")

        state = check_target_asset_state_state(d.pop("state"))




        def _parse_expected_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        expected_bytes = _parse_expected_bytes(d.pop("expected_bytes", UNSET))


        def _parse_imported_image_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        imported_image_digest = _parse_imported_image_digest(d.pop("imported_image_digest", UNSET))


        def _parse_missing_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        missing_bytes = _parse_missing_bytes(d.pop("missing_bytes", UNSET))


        present_bytes = d.pop("present_bytes", UNSET)

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))


        def _parse_verified_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                verified_at_type_0 = datetime.datetime.fromisoformat(data)



                return verified_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        verified_at = _parse_verified_at(d.pop("verified_at", UNSET))


        def _parse_verified_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        verified_sha256 = _parse_verified_sha256(d.pop("verified_sha256", UNSET))


        target_asset_state = cls(
            node_id=node_id,
            state=state,
            expected_bytes=expected_bytes,
            imported_image_digest=imported_image_digest,
            missing_bytes=missing_bytes,
            present_bytes=present_bytes,
            reason=reason,
            verified_at=verified_at,
            verified_sha256=verified_sha256,
        )

        return target_asset_state
