from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.recipe_library_import_request_document import RecipeLibraryImportRequestDocument





T = TypeVar("T", bound="RecipeLibraryImportRequest")



@_attrs_define
class RecipeLibraryImportRequest:
    """
        Attributes:
            document (RecipeLibraryImportRequestDocument):
            expected_content_sha256 (str):
            library_commit (str):
            source_path (str):
     """

    document: 'RecipeLibraryImportRequestDocument'
    expected_content_sha256: str
    library_commit: str
    source_path: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_library_import_request_document import RecipeLibraryImportRequestDocument
        document = self.document.to_dict()

        expected_content_sha256 = self.expected_content_sha256

        library_commit = self.library_commit

        source_path = self.source_path


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "document": document,
            "expected_content_sha256": expected_content_sha256,
            "library_commit": library_commit,
            "source_path": source_path,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_library_import_request_document import RecipeLibraryImportRequestDocument
        d = dict(src_dict)
        document = RecipeLibraryImportRequestDocument.from_dict(d.pop("document"))




        expected_content_sha256 = d.pop("expected_content_sha256")

        library_commit = d.pop("library_commit")

        source_path = d.pop("source_path")

        recipe_library_import_request = cls(
            document=document,
            expected_content_sha256=expected_content_sha256,
            library_commit=library_commit,
            source_path=source_path,
        )

        return recipe_library_import_request
