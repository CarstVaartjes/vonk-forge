from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, Union, cast

if TYPE_CHECKING:
  from ..models.model_cache_eviction_entry import ModelCacheEvictionEntry
  from ..models.cache_storage_response import CacheStorageResponse





T = TypeVar("T", bound="ModelCacheEvictionPreviewResponse")



@_attrs_define
class ModelCacheEvictionPreviewResponse:
    """
        Attributes:
            blockers (list[str]):
            plan_digest (str):
            protected_entries (list['ModelCacheEvictionEntry']):
            reclaimable_bytes (int):
            selected (list['ModelCacheEvictionEntry']):
            selected_bytes (int):
            storage_after (CacheStorageResponse):
            storage_before (CacheStorageResponse):
            target_bytes (int):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    blockers: list[str]
    plan_digest: str
    protected_entries: list['ModelCacheEvictionEntry']
    reclaimable_bytes: int
    selected: list['ModelCacheEvictionEntry']
    selected_bytes: int
    storage_after: 'CacheStorageResponse'
    storage_before: 'CacheStorageResponse'
    target_bytes: int
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.model_cache_eviction_entry import ModelCacheEvictionEntry
        from ..models.cache_storage_response import CacheStorageResponse
        blockers = self.blockers



        plan_digest = self.plan_digest

        protected_entries = []
        for protected_entries_item_data in self.protected_entries:
            protected_entries_item = protected_entries_item_data.to_dict()
            protected_entries.append(protected_entries_item)



        reclaimable_bytes = self.reclaimable_bytes

        selected = []
        for selected_item_data in self.selected:
            selected_item = selected_item_data.to_dict()
            selected.append(selected_item)



        selected_bytes = self.selected_bytes

        storage_after = self.storage_after.to_dict()

        storage_before = self.storage_before.to_dict()

        target_bytes = self.target_bytes

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "blockers": blockers,
            "plan_digest": plan_digest,
            "protected_entries": protected_entries,
            "reclaimable_bytes": reclaimable_bytes,
            "selected": selected,
            "selected_bytes": selected_bytes,
            "storage_after": storage_after,
            "storage_before": storage_before,
            "target_bytes": target_bytes,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.model_cache_eviction_entry import ModelCacheEvictionEntry
        from ..models.cache_storage_response import CacheStorageResponse
        d = dict(src_dict)
        blockers = cast(list[str], d.pop("blockers"))


        plan_digest = d.pop("plan_digest")

        protected_entries = []
        _protected_entries = d.pop("protected_entries")
        for protected_entries_item_data in (_protected_entries):
            protected_entries_item = ModelCacheEvictionEntry.from_dict(protected_entries_item_data)



            protected_entries.append(protected_entries_item)


        reclaimable_bytes = d.pop("reclaimable_bytes")

        selected = []
        _selected = d.pop("selected")
        for selected_item_data in (_selected):
            selected_item = ModelCacheEvictionEntry.from_dict(selected_item_data)



            selected.append(selected_item)


        selected_bytes = d.pop("selected_bytes")

        storage_after = CacheStorageResponse.from_dict(d.pop("storage_after"))




        storage_before = CacheStorageResponse.from_dict(d.pop("storage_before"))




        target_bytes = d.pop("target_bytes")

        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        model_cache_eviction_preview_response = cls(
            blockers=blockers,
            plan_digest=plan_digest,
            protected_entries=protected_entries,
            reclaimable_bytes=reclaimable_bytes,
            selected=selected,
            selected_bytes=selected_bytes,
            storage_after=storage_after,
            storage_before=storage_before,
            target_bytes=target_bytes,
            schema_version=schema_version,
        )

        return model_cache_eviction_preview_response
