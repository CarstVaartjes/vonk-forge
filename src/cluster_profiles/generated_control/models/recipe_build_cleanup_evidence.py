from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import Literal, cast






T = TypeVar("T", bound="RecipeBuildCleanupEvidence")



@_attrs_define
class RecipeBuildCleanupEvidence:
    """
        Attributes:
            build_id (str):
            operation_id (str):
            schema_version (Literal[1]):
            stopped (bool):
     """

    build_id: str
    operation_id: str
    schema_version: Literal[1]
    stopped: bool





    def to_dict(self) -> dict[str, Any]:
        build_id = self.build_id

        operation_id = self.operation_id

        schema_version = self.schema_version

        stopped = self.stopped


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "build_id": build_id,
            "operation_id": operation_id,
            "schema_version": schema_version,
            "stopped": stopped,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        build_id = d.pop("build_id")

        operation_id = d.pop("operation_id")

        schema_version = cast(Literal[1] , d.pop("schema_version"))
        if schema_version != 1:
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        stopped = d.pop("stopped")

        recipe_build_cleanup_evidence = cls(
            build_id=build_id,
            operation_id=operation_id,
            schema_version=schema_version,
            stopped=stopped,
        )

        return recipe_build_cleanup_evidence
