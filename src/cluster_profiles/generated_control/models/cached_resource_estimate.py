from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="CachedResourceEstimate")



@_attrs_define
class CachedResourceEstimate:
    """ What loading the resolved revision needs; ``None`` is unknown.

        Attributes:
            additional_disk_bytes (int | None | Unset):
            image_bytes (int | None | Unset):
            model_bytes (int | None | Unset):
            per_spark_memory_bytes (int | None | Unset):
     """

    additional_disk_bytes: int | None | Unset = UNSET
    image_bytes: int | None | Unset = UNSET
    model_bytes: int | None | Unset = UNSET
    per_spark_memory_bytes: int | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        additional_disk_bytes: int | None | Unset
        if isinstance(self.additional_disk_bytes, Unset):
            additional_disk_bytes = UNSET
        else:
            additional_disk_bytes = self.additional_disk_bytes

        image_bytes: int | None | Unset
        if isinstance(self.image_bytes, Unset):
            image_bytes = UNSET
        else:
            image_bytes = self.image_bytes

        model_bytes: int | None | Unset
        if isinstance(self.model_bytes, Unset):
            model_bytes = UNSET
        else:
            model_bytes = self.model_bytes

        per_spark_memory_bytes: int | None | Unset
        if isinstance(self.per_spark_memory_bytes, Unset):
            per_spark_memory_bytes = UNSET
        else:
            per_spark_memory_bytes = self.per_spark_memory_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if additional_disk_bytes is not UNSET:
            field_dict["additional_disk_bytes"] = additional_disk_bytes
        if image_bytes is not UNSET:
            field_dict["image_bytes"] = image_bytes
        if model_bytes is not UNSET:
            field_dict["model_bytes"] = model_bytes
        if per_spark_memory_bytes is not UNSET:
            field_dict["per_spark_memory_bytes"] = per_spark_memory_bytes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        def _parse_additional_disk_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        additional_disk_bytes = _parse_additional_disk_bytes(d.pop("additional_disk_bytes", UNSET))


        def _parse_image_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        image_bytes = _parse_image_bytes(d.pop("image_bytes", UNSET))


        def _parse_model_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        model_bytes = _parse_model_bytes(d.pop("model_bytes", UNSET))


        def _parse_per_spark_memory_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        per_spark_memory_bytes = _parse_per_spark_memory_bytes(d.pop("per_spark_memory_bytes", UNSET))


        cached_resource_estimate = cls(
            additional_disk_bytes=additional_disk_bytes,
            image_bytes=image_bytes,
            model_bytes=model_bytes,
            per_spark_memory_bytes=per_spark_memory_bytes,
        )

        return cached_resource_estimate
