from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import Literal, cast






T = TypeVar("T", bound="Spark3542CompatibilityRecoveryTarget")



@_attrs_define
class Spark3542CompatibilityRecoveryTarget:
    """
        Attributes:
            package_sha256 (str):
            package_version (Literal['0.1.0~dev.381+ga122909feaa3']):
            target_binary_digest (str):
            target_build_digest (str):
     """

    package_sha256: str
    package_version: Literal['0.1.0~dev.381+ga122909feaa3']
    target_binary_digest: str
    target_build_digest: str





    def to_dict(self) -> dict[str, Any]:
        package_sha256 = self.package_sha256

        package_version = self.package_version

        target_binary_digest = self.target_binary_digest

        target_build_digest = self.target_build_digest


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "package_sha256": package_sha256,
            "package_version": package_version,
            "target_binary_digest": target_binary_digest,
            "target_build_digest": target_build_digest,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        package_sha256 = d.pop("package_sha256")

        package_version = cast(Literal['0.1.0~dev.381+ga122909feaa3'] , d.pop("package_version"))
        if package_version != '0.1.0~dev.381+ga122909feaa3':
            raise ValueError(f"package_version must match const '0.1.0~dev.381+ga122909feaa3', got '{package_version}'")

        target_binary_digest = d.pop("target_binary_digest")

        target_build_digest = d.pop("target_build_digest")

        spark_3542_compatibility_recovery_target = cls(
            package_sha256=package_sha256,
            package_version=package_version,
            target_binary_digest=target_binary_digest,
            target_build_digest=target_build_digest,
        )

        return spark_3542_compatibility_recovery_target
