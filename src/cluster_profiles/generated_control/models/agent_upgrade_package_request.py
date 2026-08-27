from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import Literal, cast






T = TypeVar("T", bound="AgentUpgradePackageRequest")



@_attrs_define
class AgentUpgradePackageRequest:
    """
        Attributes:
            architecture (Literal['linux-arm64']):
            package_bytes (int):
            package_sha256 (str):
            package_signature (str):
            package_url (str):
            package_version (str):
            schema_version (Literal[1]):
            target_binary_digest (str):
            target_build_digest (str):
     """

    architecture: Literal['linux-arm64']
    package_bytes: int
    package_sha256: str
    package_signature: str
    package_url: str
    package_version: str
    schema_version: Literal[1]
    target_binary_digest: str
    target_build_digest: str





    def to_dict(self) -> dict[str, Any]:
        architecture = self.architecture

        package_bytes = self.package_bytes

        package_sha256 = self.package_sha256

        package_signature = self.package_signature

        package_url = self.package_url

        package_version = self.package_version

        schema_version = self.schema_version

        target_binary_digest = self.target_binary_digest

        target_build_digest = self.target_build_digest


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "architecture": architecture,
            "package_bytes": package_bytes,
            "package_sha256": package_sha256,
            "package_signature": package_signature,
            "package_url": package_url,
            "package_version": package_version,
            "schema_version": schema_version,
            "target_binary_digest": target_binary_digest,
            "target_build_digest": target_build_digest,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        architecture = cast(Literal['linux-arm64'] , d.pop("architecture"))
        if architecture != 'linux-arm64':
            raise ValueError(f"architecture must match const 'linux-arm64', got '{architecture}'")

        package_bytes = d.pop("package_bytes")

        package_sha256 = d.pop("package_sha256")

        package_signature = d.pop("package_signature")

        package_url = d.pop("package_url")

        package_version = d.pop("package_version")

        schema_version = cast(Literal[1] , d.pop("schema_version"))
        if schema_version != 1:
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        target_binary_digest = d.pop("target_binary_digest")

        target_build_digest = d.pop("target_build_digest")

        agent_upgrade_package_request = cls(
            architecture=architecture,
            package_bytes=package_bytes,
            package_sha256=package_sha256,
            package_signature=package_signature,
            package_url=package_url,
            package_version=package_version,
            schema_version=schema_version,
            target_binary_digest=target_binary_digest,
            target_build_digest=target_build_digest,
        )

        return agent_upgrade_package_request
