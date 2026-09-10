from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="LibraryResourceProjection")



@_attrs_define
class LibraryResourceProjection:
    """ Declared resource facts; unknown values remain null.

        Attributes:
            disk_bytes (Union[None, Unset, int]):
            image_bytes (Union[None, Unset, int]):
            memory_bytes (Union[None, Unset, int]):
            runtime_memory_bytes (Union[None, Unset, int]):
     """

    disk_bytes: Union[None, Unset, int] = UNSET
    image_bytes: Union[None, Unset, int] = UNSET
    memory_bytes: Union[None, Unset, int] = UNSET
    runtime_memory_bytes: Union[None, Unset, int] = UNSET





    def to_dict(self) -> dict[str, Any]:
        disk_bytes: Union[None, Unset, int]
        if isinstance(self.disk_bytes, Unset):
            disk_bytes = UNSET
        else:
            disk_bytes = self.disk_bytes

        image_bytes: Union[None, Unset, int]
        if isinstance(self.image_bytes, Unset):
            image_bytes = UNSET
        else:
            image_bytes = self.image_bytes

        memory_bytes: Union[None, Unset, int]
        if isinstance(self.memory_bytes, Unset):
            memory_bytes = UNSET
        else:
            memory_bytes = self.memory_bytes

        runtime_memory_bytes: Union[None, Unset, int]
        if isinstance(self.runtime_memory_bytes, Unset):
            runtime_memory_bytes = UNSET
        else:
            runtime_memory_bytes = self.runtime_memory_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if disk_bytes is not UNSET:
            field_dict["disk_bytes"] = disk_bytes
        if image_bytes is not UNSET:
            field_dict["image_bytes"] = image_bytes
        if memory_bytes is not UNSET:
            field_dict["memory_bytes"] = memory_bytes
        if runtime_memory_bytes is not UNSET:
            field_dict["runtime_memory_bytes"] = runtime_memory_bytes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        def _parse_disk_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        disk_bytes = _parse_disk_bytes(d.pop("disk_bytes", UNSET))


        def _parse_image_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        image_bytes = _parse_image_bytes(d.pop("image_bytes", UNSET))


        def _parse_memory_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        memory_bytes = _parse_memory_bytes(d.pop("memory_bytes", UNSET))


        def _parse_runtime_memory_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        runtime_memory_bytes = _parse_runtime_memory_bytes(d.pop("runtime_memory_bytes", UNSET))


        library_resource_projection = cls(
            disk_bytes=disk_bytes,
            image_bytes=image_bytes,
            memory_bytes=memory_bytes,
            runtime_memory_bytes=runtime_memory_bytes,
        )

        return library_resource_projection
