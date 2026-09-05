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
  from ..models.model_cache_update_response import ModelCacheUpdateResponse





T = TypeVar("T", bound="ModelCacheUpdatesResponse")



@_attrs_define
class ModelCacheUpdatesResponse:
    """
        Attributes:
            total (int):
            updates (list['ModelCacheUpdateResponse']):
            next_cursor (Union[None, Unset, str]):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
            source_policy (Union[Literal['nas-first'], Unset]):  Default: 'nas-first'.
     """

    total: int
    updates: list['ModelCacheUpdateResponse']
    next_cursor: Union[None, Unset, str] = UNSET
    schema_version: Union[Literal[2], Unset] = 2
    source_policy: Union[Literal['nas-first'], Unset] = 'nas-first'





    def to_dict(self) -> dict[str, Any]:
        from ..models.model_cache_update_response import ModelCacheUpdateResponse
        total = self.total

        updates = []
        for updates_item_data in self.updates:
            updates_item = updates_item_data.to_dict()
            updates.append(updates_item)



        next_cursor: Union[None, Unset, str]
        if isinstance(self.next_cursor, Unset):
            next_cursor = UNSET
        else:
            next_cursor = self.next_cursor

        schema_version = self.schema_version

        source_policy = self.source_policy


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "total": total,
            "updates": updates,
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
        from ..models.model_cache_update_response import ModelCacheUpdateResponse
        d = dict(src_dict)
        total = d.pop("total")

        updates = []
        _updates = d.pop("updates")
        for updates_item_data in (_updates):
            updates_item = ModelCacheUpdateResponse.from_dict(updates_item_data)



            updates.append(updates_item)


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

        model_cache_updates_response = cls(
            total=total,
            updates=updates,
            next_cursor=next_cursor,
            schema_version=schema_version,
            source_policy=source_policy,
        )

        return model_cache_updates_response
