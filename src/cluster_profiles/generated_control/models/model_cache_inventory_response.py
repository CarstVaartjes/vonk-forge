from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.cache_entry_response import CacheEntryResponse
  from ..models.cache_storage_response import CacheStorageResponse





T = TypeVar("T", bound="ModelCacheInventoryResponse")



@_attrs_define
class ModelCacheInventoryResponse:
    """
        Attributes:
            entries (list['CacheEntryResponse']):
            storage (CacheStorageResponse):
            total (int):
            next_cursor (Union[None, Unset, str]):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
            source_policy (Union[Literal['nas-first'], Unset]):  Default: 'nas-first'.
     """

    entries: list['CacheEntryResponse']
    storage: 'CacheStorageResponse'
    total: int
    next_cursor: Union[None, Unset, str] = UNSET
    schema_version: Union[Literal[2], Unset] = 2
    source_policy: Union[Literal['nas-first'], Unset] = 'nas-first'





    def to_dict(self) -> dict[str, Any]:
        from ..models.cache_entry_response import CacheEntryResponse
        from ..models.cache_storage_response import CacheStorageResponse
        entries = []
        for entries_item_data in self.entries:
            entries_item = entries_item_data.to_dict()
            entries.append(entries_item)



        storage = self.storage.to_dict()

        total = self.total

        next_cursor: Union[None, Unset, str]
        if isinstance(self.next_cursor, Unset):
            next_cursor = UNSET
        else:
            next_cursor = self.next_cursor

        schema_version = self.schema_version

        source_policy = self.source_policy


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "entries": entries,
            "storage": storage,
            "total": total,
        })
        if next_cursor is not UNSET:
            field_dict["next_cursor"] = next_cursor
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if source_policy is not UNSET:
            field_dict["source_policy"] = source_policy

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.cache_entry_response import CacheEntryResponse
        from ..models.cache_storage_response import CacheStorageResponse
        d = dict(src_dict)
        entries = []
        _entries = d.pop("entries")
        for entries_item_data in (_entries):
            entries_item = CacheEntryResponse.from_dict(entries_item_data)



            entries.append(entries_item)


        storage = CacheStorageResponse.from_dict(d.pop("storage"))




        total = d.pop("total")

        def _parse_next_cursor(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        next_cursor = _parse_next_cursor(d.pop("next_cursor", UNSET))


        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        source_policy = cast(Union[Literal['nas-first'], Unset] , d.pop("source_policy", UNSET))
        if source_policy != 'nas-first' and not isinstance(source_policy, Unset):
            raise ValueError(f"source_policy must match const 'nas-first', got '{source_policy}'")

        model_cache_inventory_response = cls(
            entries=entries,
            storage=storage,
            total=total,
            next_cursor=next_cursor,
            schema_version=schema_version,
            source_policy=source_policy,
        )

        return model_cache_inventory_response
