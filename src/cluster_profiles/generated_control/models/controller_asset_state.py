from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.controller_asset_state_source import check_controller_asset_state_source
from ..models.controller_asset_state_source import ControllerAssetStateSource
from ..models.controller_asset_state_state import check_controller_asset_state_state
from ..models.controller_asset_state_state import ControllerAssetStateState
from ..types import UNSET, Unset
from typing import cast
import datetime






T = TypeVar("T", bound="ControllerAssetState")



@_attrs_define
class ControllerAssetState:
    """ Availability of one immutable asset in Controller/NAS storage.

        Attributes:
            source (ControllerAssetStateSource):
            state (ControllerAssetStateState):
            expected_bytes (int | None | Unset):
            missing_bytes (int | None | Unset):
            reason (None | str | Unset):
            verified_at (datetime.datetime | None | Unset):
            verified_bytes (int | Unset):  Default: 0.
            verified_sha256 (None | str | Unset):
     """

    source: ControllerAssetStateSource
    state: ControllerAssetStateState
    expected_bytes: int | None | Unset = UNSET
    missing_bytes: int | None | Unset = UNSET
    reason: None | str | Unset = UNSET
    verified_at: datetime.datetime | None | Unset = UNSET
    verified_bytes: int | Unset = 0
    verified_sha256: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        source: str = self.source

        state: str = self.state

        expected_bytes: int | None | Unset
        if isinstance(self.expected_bytes, Unset):
            expected_bytes = UNSET
        else:
            expected_bytes = self.expected_bytes

        missing_bytes: int | None | Unset
        if isinstance(self.missing_bytes, Unset):
            missing_bytes = UNSET
        else:
            missing_bytes = self.missing_bytes

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

        verified_bytes = self.verified_bytes

        verified_sha256: None | str | Unset
        if isinstance(self.verified_sha256, Unset):
            verified_sha256 = UNSET
        else:
            verified_sha256 = self.verified_sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "source": source,
            "state": state,
        })
        if expected_bytes is not UNSET:
            field_dict["expected_bytes"] = expected_bytes
        if missing_bytes is not UNSET:
            field_dict["missing_bytes"] = missing_bytes
        if reason is not UNSET:
            field_dict["reason"] = reason
        if verified_at is not UNSET:
            field_dict["verified_at"] = verified_at
        if verified_bytes is not UNSET:
            field_dict["verified_bytes"] = verified_bytes
        if verified_sha256 is not UNSET:
            field_dict["verified_sha256"] = verified_sha256

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        source = check_controller_asset_state_source(d.pop("source"))




        state = check_controller_asset_state_state(d.pop("state"))




        def _parse_expected_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        expected_bytes = _parse_expected_bytes(d.pop("expected_bytes", UNSET))


        def _parse_missing_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        missing_bytes = _parse_missing_bytes(d.pop("missing_bytes", UNSET))


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


        verified_bytes = d.pop("verified_bytes", UNSET)

        def _parse_verified_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        verified_sha256 = _parse_verified_sha256(d.pop("verified_sha256", UNSET))


        controller_asset_state = cls(
            source=source,
            state=state,
            expected_bytes=expected_bytes,
            missing_bytes=missing_bytes,
            reason=reason,
            verified_at=verified_at,
            verified_bytes=verified_bytes,
            verified_sha256=verified_sha256,
        )

        return controller_asset_state
