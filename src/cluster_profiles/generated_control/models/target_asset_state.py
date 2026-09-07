from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.target_asset_state_state import check_target_asset_state_state
from ..models.target_asset_state_state import TargetAssetStateState
from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Union
import datetime






T = TypeVar("T", bound="TargetAssetState")



@_attrs_define
class TargetAssetState:
    """ Staging and verification state for one immutable asset on one Spark.

        Attributes:
            node_id (str):
            state (TargetAssetStateState):
            expected_bytes (Union[None, Unset, int]):
            imported_image_digest (Union[None, Unset, str]):
            missing_bytes (Union[None, Unset, int]):
            present_bytes (Union[Unset, int]):  Default: 0.
            reason (Union[None, Unset, str]):
            verified_at (Union[None, Unset, datetime.datetime]):
            verified_sha256 (Union[None, Unset, str]):
     """

    node_id: str
    state: TargetAssetStateState
    expected_bytes: Union[None, Unset, int] = UNSET
    imported_image_digest: Union[None, Unset, str] = UNSET
    missing_bytes: Union[None, Unset, int] = UNSET
    present_bytes: Union[Unset, int] = 0
    reason: Union[None, Unset, str] = UNSET
    verified_at: Union[None, Unset, datetime.datetime] = UNSET
    verified_sha256: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        node_id = self.node_id

        state: str = self.state

        expected_bytes: Union[None, Unset, int]
        if isinstance(self.expected_bytes, Unset):
            expected_bytes = UNSET
        else:
            expected_bytes = self.expected_bytes

        imported_image_digest: Union[None, Unset, str]
        if isinstance(self.imported_image_digest, Unset):
            imported_image_digest = UNSET
        else:
            imported_image_digest = self.imported_image_digest

        missing_bytes: Union[None, Unset, int]
        if isinstance(self.missing_bytes, Unset):
            missing_bytes = UNSET
        else:
            missing_bytes = self.missing_bytes

        present_bytes = self.present_bytes

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

        verified_sha256: Union[None, Unset, str]
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




        def _parse_expected_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        expected_bytes = _parse_expected_bytes(d.pop("expected_bytes", UNSET))


        def _parse_imported_image_digest(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        imported_image_digest = _parse_imported_image_digest(d.pop("imported_image_digest", UNSET))


        def _parse_missing_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        missing_bytes = _parse_missing_bytes(d.pop("missing_bytes", UNSET))


        present_bytes = d.pop("present_bytes", UNSET)

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


        def _parse_verified_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

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
