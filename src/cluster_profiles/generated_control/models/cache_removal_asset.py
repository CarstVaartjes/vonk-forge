from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.cache_removal_asset_availability import CacheRemovalAssetAvailability
from ..models.cache_removal_asset_availability import check_cache_removal_asset_availability
from ..models.cache_removal_asset_disposition import CacheRemovalAssetDisposition
from ..models.cache_removal_asset_disposition import check_cache_removal_asset_disposition
from ..models.cache_removal_asset_kind import CacheRemovalAssetKind
from ..models.cache_removal_asset_kind import check_cache_removal_asset_kind
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="CacheRemovalAsset")



@_attrs_define
class CacheRemovalAsset:
    """ One exact cache identity and its owner-reported storage condition.

        Attributes:
            availability (CacheRemovalAssetAvailability):
            disposition (CacheRemovalAssetDisposition):
            kind (CacheRemovalAssetKind):
            sha256 (str):
            available_bytes (Union[None, Unset, int]):
            expected_bytes (Union[None, Unset, int]):
     """

    availability: CacheRemovalAssetAvailability
    disposition: CacheRemovalAssetDisposition
    kind: CacheRemovalAssetKind
    sha256: str
    available_bytes: Union[None, Unset, int] = UNSET
    expected_bytes: Union[None, Unset, int] = UNSET





    def to_dict(self) -> dict[str, Any]:
        availability: str = self.availability

        disposition: str = self.disposition

        kind: str = self.kind

        sha256 = self.sha256

        available_bytes: Union[None, Unset, int]
        if isinstance(self.available_bytes, Unset):
            available_bytes = UNSET
        else:
            available_bytes = self.available_bytes

        expected_bytes: Union[None, Unset, int]
        if isinstance(self.expected_bytes, Unset):
            expected_bytes = UNSET
        else:
            expected_bytes = self.expected_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "availability": availability,
            "disposition": disposition,
            "kind": kind,
            "sha256": sha256,
        })
        if available_bytes is not UNSET:
            field_dict["available_bytes"] = available_bytes
        if expected_bytes is not UNSET:
            field_dict["expected_bytes"] = expected_bytes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        availability = check_cache_removal_asset_availability(d.pop("availability"))




        disposition = check_cache_removal_asset_disposition(d.pop("disposition"))




        kind = check_cache_removal_asset_kind(d.pop("kind"))




        sha256 = d.pop("sha256")

        def _parse_available_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        available_bytes = _parse_available_bytes(d.pop("available_bytes", UNSET))


        def _parse_expected_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        expected_bytes = _parse_expected_bytes(d.pop("expected_bytes", UNSET))


        cache_removal_asset = cls(
            availability=availability,
            disposition=disposition,
            kind=kind,
            sha256=sha256,
            available_bytes=available_bytes,
            expected_bytes=expected_bytes,
        )

        return cache_removal_asset
