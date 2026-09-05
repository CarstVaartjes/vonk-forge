from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import Literal, Union, cast






T = TypeVar("T", bound="ModelCacheRepairRequest")



@_attrs_define
class ModelCacheRepairRequest:
    """
        Attributes:
            artifact_set_sha256 (str):
            plan_digest (str):
            request_key (str):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
            source_policy (Union[Literal['nas-first'], Unset]):  Default: 'nas-first'.
     """

    artifact_set_sha256: str
    plan_digest: str
    request_key: str
    schema_version: Union[Literal[2], Unset] = 2
    source_policy: Union[Literal['nas-first'], Unset] = 'nas-first'





    def to_dict(self) -> dict[str, Any]:
        artifact_set_sha256 = self.artifact_set_sha256

        plan_digest = self.plan_digest

        request_key = self.request_key

        schema_version = self.schema_version

        source_policy = self.source_policy


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifact_set_sha256": artifact_set_sha256,
            "plan_digest": plan_digest,
            "request_key": request_key,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if source_policy is not UNSET:
            field_dict["source_policy"] = source_policy

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        artifact_set_sha256 = d.pop("artifact_set_sha256")

        plan_digest = d.pop("plan_digest")

        request_key = d.pop("request_key")

        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        source_policy = cast(Union[Literal['nas-first'], Unset] , d.pop("source_policy", UNSET))
        if source_policy != 'nas-first' and not isinstance(source_policy, Unset):
            raise ValueError(f"source_policy must match const 'nas-first', got '{source_policy}'")

        model_cache_repair_request = cls(
            artifact_set_sha256=artifact_set_sha256,
            plan_digest=plan_digest,
            request_key=request_key,
            schema_version=schema_version,
            source_policy=source_policy,
        )

        return model_cache_repair_request
