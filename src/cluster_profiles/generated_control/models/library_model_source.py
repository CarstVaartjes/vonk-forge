from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="LibraryModelSource")



@_attrs_define
class LibraryModelSource:
    """
        Attributes:
            repository (str):
            revision (str):
     """

    repository: str
    revision: str





    def to_dict(self) -> dict[str, Any]:
        repository = self.repository

        revision = self.revision


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "repository": repository,
            "revision": revision,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        repository = d.pop("repository")

        revision = d.pop("revision")

        library_model_source = cls(
            repository=repository,
            revision=revision,
        )

        return library_model_source
