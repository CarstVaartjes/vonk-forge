from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.model_cache_repair_preview_response_current_state import check_model_cache_repair_preview_response_current_state
from ..models.model_cache_repair_preview_response_current_state import ModelCacheRepairPreviewResponseCurrentState
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, Union, cast






T = TypeVar("T", bound="ModelCacheRepairPreviewResponse")



@_attrs_define
class ModelCacheRepairPreviewResponse:
    """
        Attributes:
            artifact_count (int):
            artifact_set_sha256 (str):
            current_state (ModelCacheRepairPreviewResponseCurrentState):
            expected_bytes (int):
            plan_digest (str):
            verified_bytes (int):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
            source_policy (Union[Literal['nas-first'], Unset]):  Default: 'nas-first'.
     """

    artifact_count: int
    artifact_set_sha256: str
    current_state: ModelCacheRepairPreviewResponseCurrentState
    expected_bytes: int
    plan_digest: str
    verified_bytes: int
    schema_version: Union[Literal[2], Unset] = 2
    source_policy: Union[Literal['nas-first'], Unset] = 'nas-first'





    def to_dict(self) -> dict[str, Any]:
        artifact_count = self.artifact_count

        artifact_set_sha256 = self.artifact_set_sha256

        current_state: str = self.current_state

        expected_bytes = self.expected_bytes

        plan_digest = self.plan_digest

        verified_bytes = self.verified_bytes

        schema_version = self.schema_version

        source_policy = self.source_policy


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifact_count": artifact_count,
            "artifact_set_sha256": artifact_set_sha256,
            "current_state": current_state,
            "expected_bytes": expected_bytes,
            "plan_digest": plan_digest,
            "verified_bytes": verified_bytes,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if source_policy is not UNSET:
            field_dict["source_policy"] = source_policy

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        artifact_count = d.pop("artifact_count")

        artifact_set_sha256 = d.pop("artifact_set_sha256")

        current_state = check_model_cache_repair_preview_response_current_state(d.pop("current_state"))




        expected_bytes = d.pop("expected_bytes")

        plan_digest = d.pop("plan_digest")

        verified_bytes = d.pop("verified_bytes")

        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        source_policy = cast(Union[Literal['nas-first'], Unset] , d.pop("source_policy", UNSET))
        if source_policy != 'nas-first' and not isinstance(source_policy, Unset):
            raise ValueError(f"source_policy must match const 'nas-first', got '{source_policy}'")

        model_cache_repair_preview_response = cls(
            artifact_count=artifact_count,
            artifact_set_sha256=artifact_set_sha256,
            current_state=current_state,
            expected_bytes=expected_bytes,
            plan_digest=plan_digest,
            verified_bytes=verified_bytes,
            schema_version=schema_version,
            source_policy=source_policy,
        )

        return model_cache_repair_preview_response
