from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="PublicImportRequest")



@_attrs_define
class PublicImportRequest:
    """
        Attributes:
            expected_content_sha256 (str):
            uri (str):
     """

    expected_content_sha256: str
    uri: str





    def to_dict(self) -> dict[str, Any]:
        expected_content_sha256 = self.expected_content_sha256

        uri = self.uri


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "expected_content_sha256": expected_content_sha256,
            "uri": uri,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        expected_content_sha256 = d.pop("expected_content_sha256")

        uri = d.pop("uri")

        public_import_request = cls(
            expected_content_sha256=expected_content_sha256,
            uri=uri,
        )

        return public_import_request
