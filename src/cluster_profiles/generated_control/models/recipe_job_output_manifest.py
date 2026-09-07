from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.recipe_job_file import RecipeJobFile





T = TypeVar("T", bound="RecipeJobOutputManifest")



@_attrs_define
class RecipeJobOutputManifest:
    """
        Attributes:
            files (list['RecipeJobFile']):
            manifest_sha256 (str):
            schema_version (Literal[1]):
            total_bytes (int):
     """

    files: list['RecipeJobFile']
    manifest_sha256: str
    schema_version: Literal[1]
    total_bytes: int





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_job_file import RecipeJobFile
        files = []
        for files_item_data in self.files:
            files_item = files_item_data.to_dict()
            files.append(files_item)



        manifest_sha256 = self.manifest_sha256

        schema_version = self.schema_version

        total_bytes = self.total_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "files": files,
            "manifest_sha256": manifest_sha256,
            "schema_version": schema_version,
            "total_bytes": total_bytes,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_job_file import RecipeJobFile
        d = dict(src_dict)
        files = []
        _files = d.pop("files")
        for files_item_data in (_files):
            files_item = RecipeJobFile.from_dict(files_item_data)



            files.append(files_item)


        manifest_sha256 = d.pop("manifest_sha256")

        schema_version = cast(Literal[1] , d.pop("schema_version"))
        if schema_version != 1:
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        total_bytes = d.pop("total_bytes")

        recipe_job_output_manifest = cls(
            files=files,
            manifest_sha256=manifest_sha256,
            schema_version=schema_version,
            total_bytes=total_bytes,
        )

        return recipe_job_output_manifest
