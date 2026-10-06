from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.model_cache_transfer_artifacts import ModelCacheTransferArtifacts





T = TypeVar("T", bound="ModelCacheTransfer")



@_attrs_define
class ModelCacheTransfer:
    """
        Attributes:
            artifacts (ModelCacheTransferArtifacts):
            total_bytes (int):
            schema_version (Literal[2] | Unset):  Default: 2.
     """

    artifacts: ModelCacheTransferArtifacts
    total_bytes: int
    schema_version: Literal[2] | Unset = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.model_cache_transfer_artifacts import ModelCacheTransferArtifacts # noqa: PLC0415
        artifacts = self.artifacts.to_dict()

        total_bytes = self.total_bytes

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifacts": artifacts,
            "total_bytes": total_bytes,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.model_cache_transfer_artifacts import ModelCacheTransferArtifacts # noqa: PLC0415
        d = dict(src_dict)
        artifacts = ModelCacheTransferArtifacts.from_dict(d.pop("artifacts"))




        total_bytes = d.pop("total_bytes")

        schema_version = cast(Literal[2] | Unset , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        model_cache_transfer = cls(
            artifacts=artifacts,
            total_bytes=total_bytes,
            schema_version=schema_version,
        )

        return model_cache_transfer
