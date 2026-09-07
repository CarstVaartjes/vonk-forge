from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="ModelCacheEvictionResult")



@_attrs_define
class ModelCacheEvictionResult:
    """
        Attributes:
            reclaimed_bytes (int):
            removed_entries (list[str]):
            schema_version (Literal[2]):
     """

    reclaimed_bytes: int
    removed_entries: list[str]
    schema_version: Literal[2]





    def to_dict(self) -> dict[str, Any]:
        reclaimed_bytes = self.reclaimed_bytes

        removed_entries = self.removed_entries



        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "reclaimed_bytes": reclaimed_bytes,
            "removed_entries": removed_entries,
            "schema_version": schema_version,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        reclaimed_bytes = d.pop("reclaimed_bytes")

        removed_entries = cast(list[str], d.pop("removed_entries"))


        schema_version = cast(Literal[2] , d.pop("schema_version"))
        if schema_version != 2:
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        model_cache_eviction_result = cls(
            reclaimed_bytes=reclaimed_bytes,
            removed_entries=removed_entries,
            schema_version=schema_version,
        )

        return model_cache_eviction_result
