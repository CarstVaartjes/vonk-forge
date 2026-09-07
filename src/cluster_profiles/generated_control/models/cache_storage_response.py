from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import Literal, Union, cast






T = TypeVar("T", bound="CacheStorageResponse")



@_attrs_define
class CacheStorageResponse:
    """
        Attributes:
            available_bytes (int):
            free_bytes (int):
            in_flight_bytes (int):
            protected_bytes (int):
            reclaimable_bytes (int):
            reserve_bytes (int):
            total_bytes (int):
            unique_used_bytes (int):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    available_bytes: int
    free_bytes: int
    in_flight_bytes: int
    protected_bytes: int
    reclaimable_bytes: int
    reserve_bytes: int
    total_bytes: int
    unique_used_bytes: int
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        available_bytes = self.available_bytes

        free_bytes = self.free_bytes

        in_flight_bytes = self.in_flight_bytes

        protected_bytes = self.protected_bytes

        reclaimable_bytes = self.reclaimable_bytes

        reserve_bytes = self.reserve_bytes

        total_bytes = self.total_bytes

        unique_used_bytes = self.unique_used_bytes

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "available_bytes": available_bytes,
            "free_bytes": free_bytes,
            "in_flight_bytes": in_flight_bytes,
            "protected_bytes": protected_bytes,
            "reclaimable_bytes": reclaimable_bytes,
            "reserve_bytes": reserve_bytes,
            "total_bytes": total_bytes,
            "unique_used_bytes": unique_used_bytes,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        available_bytes = d.pop("available_bytes")

        free_bytes = d.pop("free_bytes")

        in_flight_bytes = d.pop("in_flight_bytes")

        protected_bytes = d.pop("protected_bytes")

        reclaimable_bytes = d.pop("reclaimable_bytes")

        reserve_bytes = d.pop("reserve_bytes")

        total_bytes = d.pop("total_bytes")

        unique_used_bytes = d.pop("unique_used_bytes")

        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        cache_storage_response = cls(
            available_bytes=available_bytes,
            free_bytes=free_bytes,
            in_flight_bytes=in_flight_bytes,
            protected_bytes=protected_bytes,
            reclaimable_bytes=reclaimable_bytes,
            reserve_bytes=reserve_bytes,
            total_bytes=total_bytes,
            unique_used_bytes=unique_used_bytes,
            schema_version=schema_version,
        )

        return cache_storage_response
