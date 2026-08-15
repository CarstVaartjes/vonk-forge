from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="ResolveCatalogEntityRequest")



@_attrs_define
class ResolveCatalogEntityRequest:
    """
        Attributes:
            expected_revision (int):
     """

    expected_revision: int





    def to_dict(self) -> dict[str, Any]:
        expected_revision = self.expected_revision


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "expected_revision": expected_revision,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        expected_revision = d.pop("expected_revision")

        resolve_catalog_entity_request = cls(
            expected_revision=expected_revision,
        )

        return resolve_catalog_entity_request
