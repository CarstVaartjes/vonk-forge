from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import Literal, cast






T = TypeVar("T", bound="ModelCacheDownloadResult")



@_attrs_define
class ModelCacheDownloadResult:
    """
        Attributes:
            artifact_set_sha256 (str):
            coverage (Literal['complete']):
            schema_version (Literal[2]):
     """

    artifact_set_sha256: str
    coverage: Literal['complete']
    schema_version: Literal[2]





    def to_dict(self) -> dict[str, Any]:
        artifact_set_sha256 = self.artifact_set_sha256

        coverage = self.coverage

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifact_set_sha256": artifact_set_sha256,
            "coverage": coverage,
            "schema_version": schema_version,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        artifact_set_sha256 = d.pop("artifact_set_sha256")

        coverage = cast(Literal['complete'] , d.pop("coverage"))
        if coverage != 'complete':
            raise ValueError(f"coverage must match const 'complete', got '{coverage}'")

        schema_version = cast(Literal[2] , d.pop("schema_version"))
        if schema_version != 2:
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        model_cache_download_result = cls(
            artifact_set_sha256=artifact_set_sha256,
            coverage=coverage,
            schema_version=schema_version,
        )

        return model_cache_download_result
