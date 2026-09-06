from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import Literal, Union, cast






T = TypeVar("T", bound="ModelCacheEvictRequest")



@_attrs_define
class ModelCacheEvictRequest:
    """
        Attributes:
            plan_digest (str):
            request_key (str):
            target_bytes (int):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    plan_digest: str
    request_key: str
    target_bytes: int
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        plan_digest = self.plan_digest

        request_key = self.request_key

        target_bytes = self.target_bytes

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "plan_digest": plan_digest,
            "request_key": request_key,
            "target_bytes": target_bytes,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        plan_digest = d.pop("plan_digest")

        request_key = d.pop("request_key")

        target_bytes = d.pop("target_bytes")

        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        model_cache_evict_request = cls(
            plan_digest=plan_digest,
            request_key=request_key,
            target_bytes=target_bytes,
            schema_version=schema_version,
        )

        return model_cache_evict_request
