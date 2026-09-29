from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
import datetime






T = TypeVar("T", bound="LibraryRelease")



@_attrs_define
class LibraryRelease:
    """ The recipe library release this Controller last synchronized.

        Attributes:
            commit (str):
            updated_at (datetime.datetime):
            version (str):
     """

    commit: str
    updated_at: datetime.datetime
    version: str





    def to_dict(self) -> dict[str, Any]:
        commit = self.commit

        updated_at = self.updated_at.isoformat()

        version = self.version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "commit": commit,
            "updated_at": updated_at,
            "version": version,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        commit = d.pop("commit")

        updated_at = datetime.datetime.fromisoformat(d.pop("updated_at"))




        version = d.pop("version")

        library_release = cls(
            commit=commit,
            updated_at=updated_at,
            version=version,
        )

        return library_release
