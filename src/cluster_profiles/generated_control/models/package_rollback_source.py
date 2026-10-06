from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="PackageRollbackSource")



@_attrs_define
class PackageRollbackSource:
    """
        Attributes:
            binary_sha256 (str):
            helper_sha256 (str):
            package_sha256 (str):
            package_signature (str):
            package_version (str):
     """

    binary_sha256: str
    helper_sha256: str
    package_sha256: str
    package_signature: str
    package_version: str





    def to_dict(self) -> dict[str, Any]:
        binary_sha256 = self.binary_sha256

        helper_sha256 = self.helper_sha256

        package_sha256 = self.package_sha256

        package_signature = self.package_signature

        package_version = self.package_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "binary_sha256": binary_sha256,
            "helper_sha256": helper_sha256,
            "package_sha256": package_sha256,
            "package_signature": package_signature,
            "package_version": package_version,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        binary_sha256 = d.pop("binary_sha256")

        helper_sha256 = d.pop("helper_sha256")

        package_sha256 = d.pop("package_sha256")

        package_signature = d.pop("package_signature")

        package_version = d.pop("package_version")

        package_rollback_source = cls(
            binary_sha256=binary_sha256,
            helper_sha256=helper_sha256,
            package_sha256=package_sha256,
            package_signature=package_signature,
            package_version=package_version,
        )

        return package_rollback_source
