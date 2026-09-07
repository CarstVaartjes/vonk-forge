from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.controller_asset_state_source import check_controller_asset_state_source
from ..models.controller_asset_state_source import ControllerAssetStateSource
from ..models.controller_asset_state_state import check_controller_asset_state_state
from ..models.controller_asset_state_state import ControllerAssetStateState
from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Union
import datetime






T = TypeVar("T", bound="ControllerAssetState")



@_attrs_define
class ControllerAssetState:
    """ Availability of one immutable asset in Controller/NAS storage.

        Attributes:
            source (ControllerAssetStateSource):
            state (ControllerAssetStateState):
            expected_bytes (Union[None, Unset, int]):
            missing_bytes (Union[None, Unset, int]):
            reason (Union[None, Unset, str]):
            verified_at (Union[None, Unset, datetime.datetime]):
            verified_bytes (Union[Unset, int]):  Default: 0.
            verified_sha256 (Union[None, Unset, str]):
     """

    source: ControllerAssetStateSource
    state: ControllerAssetStateState
    expected_bytes: Union[None, Unset, int] = UNSET
    missing_bytes: Union[None, Unset, int] = UNSET
    reason: Union[None, Unset, str] = UNSET
    verified_at: Union[None, Unset, datetime.datetime] = UNSET
    verified_bytes: Union[Unset, int] = 0
    verified_sha256: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        source: str = self.source

        state: str = self.state

        expected_bytes: Union[None, Unset, int]
        if isinstance(self.expected_bytes, Unset):
            expected_bytes = UNSET
        else:
            expected_bytes = self.expected_bytes

        missing_bytes: Union[None, Unset, int]
        if isinstance(self.missing_bytes, Unset):
            missing_bytes = UNSET
        else:
            missing_bytes = self.missing_bytes

        reason: Union[None, Unset, str]
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        verified_at: Union[None, Unset, str]
        if isinstance(self.verified_at, Unset):
            verified_at = UNSET
        elif isinstance(self.verified_at, datetime.datetime):
            verified_at = self.verified_at.isoformat()
        else:
            verified_at = self.verified_at

        verified_bytes = self.verified_bytes

        verified_sha256: Union[None, Unset, str]
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




        def _parse_expected_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        expected_bytes = _parse_expected_bytes(d.pop("expected_bytes", UNSET))


        def _parse_missing_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        missing_bytes = _parse_missing_bytes(d.pop("missing_bytes", UNSET))


        def _parse_reason(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        reason = _parse_reason(d.pop("reason", UNSET))


        def _parse_verified_at(data: object) -> Union[None, Unset, datetime.datetime]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                verified_at_type_0 = isoparse(data)



                return verified_at_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, Unset, datetime.datetime], data)

        verified_at = _parse_verified_at(d.pop("verified_at", UNSET))


        verified_bytes = d.pop("verified_bytes", UNSET)

        def _parse_verified_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

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
