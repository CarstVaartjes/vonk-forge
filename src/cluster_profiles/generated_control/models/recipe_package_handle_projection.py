from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="RecipePackageHandleProjection")



@_attrs_define
class RecipePackageHandleProjection:
    """
        Attributes:
            archive_path (str):
            closure_path (str):
            package_path (str):
            package_sha256 (str):
            package_size (int):
            publication_commit (str):
            recipe_content_sha256 (str):
            source_commit (str):
     """

    archive_path: str
    closure_path: str
    package_path: str
    package_sha256: str
    package_size: int
    publication_commit: str
    recipe_content_sha256: str
    source_commit: str





    def to_dict(self) -> dict[str, Any]:
        archive_path = self.archive_path

        closure_path = self.closure_path

        package_path = self.package_path

        package_sha256 = self.package_sha256

        package_size = self.package_size

        publication_commit = self.publication_commit

        recipe_content_sha256 = self.recipe_content_sha256

        source_commit = self.source_commit


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "archive_path": archive_path,
            "closure_path": closure_path,
            "package_path": package_path,
            "package_sha256": package_sha256,
            "package_size": package_size,
            "publication_commit": publication_commit,
            "recipe_content_sha256": recipe_content_sha256,
            "source_commit": source_commit,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        archive_path = d.pop("archive_path")

        closure_path = d.pop("closure_path")

        package_path = d.pop("package_path")

        package_sha256 = d.pop("package_sha256")

        package_size = d.pop("package_size")

        publication_commit = d.pop("publication_commit")

        recipe_content_sha256 = d.pop("recipe_content_sha256")

        source_commit = d.pop("source_commit")

        recipe_package_handle_projection = cls(
            archive_path=archive_path,
            closure_path=closure_path,
            package_path=package_path,
            package_sha256=package_sha256,
            package_size=package_size,
            publication_commit=publication_commit,
            recipe_content_sha256=recipe_content_sha256,
            source_commit=source_commit,
        )

        return recipe_package_handle_projection
