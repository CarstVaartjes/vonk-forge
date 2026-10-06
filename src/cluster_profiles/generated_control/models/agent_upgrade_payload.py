from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.package_rollback_authority import PackageRollbackAuthority





T = TypeVar("T", bound="AgentUpgradePayload")



@_attrs_define
class AgentUpgradePayload:
    """ Signed package authority for the current agent upgrade operation.

        Attributes:
            architecture (Literal['linux-arm64']):
            package_bytes (int):
            package_sha256 (str):
            package_signature (str):
            package_url (str):
            package_version (str):
            rollback (PackageRollbackAuthority):
            schema_version (Literal[1]):
            source_package_bytes (int):
            source_package_url (str):
            target_binary_digest (str):
            target_build_digest (str):
     """

    architecture: Literal['linux-arm64']
    package_bytes: int
    package_sha256: str
    package_signature: str
    package_url: str
    package_version: str
    rollback: PackageRollbackAuthority
    schema_version: Literal[1]
    source_package_bytes: int
    source_package_url: str
    target_binary_digest: str
    target_build_digest: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.package_rollback_authority import PackageRollbackAuthority # noqa: PLC0415
        architecture = self.architecture

        package_bytes = self.package_bytes

        package_sha256 = self.package_sha256

        package_signature = self.package_signature

        package_url = self.package_url

        package_version = self.package_version

        rollback = self.rollback.to_dict()

        schema_version = self.schema_version

        source_package_bytes = self.source_package_bytes

        source_package_url = self.source_package_url

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
            "rollback": rollback,
            "schema_version": schema_version,
            "source_package_bytes": source_package_bytes,
            "source_package_url": source_package_url,
            "target_binary_digest": target_binary_digest,
            "target_build_digest": target_build_digest,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.package_rollback_authority import PackageRollbackAuthority # noqa: PLC0415
        d = dict(src_dict)
        architecture = cast(Literal['linux-arm64'] , d.pop("architecture"))
        if architecture != 'linux-arm64':
            raise ValueError(f"architecture must match const 'linux-arm64', got '{architecture}'")

        package_bytes = d.pop("package_bytes")

        package_sha256 = d.pop("package_sha256")

        package_signature = d.pop("package_signature")

        package_url = d.pop("package_url")

        package_version = d.pop("package_version")

        rollback = PackageRollbackAuthority.from_dict(d.pop("rollback"))




        schema_version = cast(Literal[1] , d.pop("schema_version"))
        if schema_version != 1:
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        source_package_bytes = d.pop("source_package_bytes")

        source_package_url = d.pop("source_package_url")

        target_binary_digest = d.pop("target_binary_digest")

        target_build_digest = d.pop("target_build_digest")

        agent_upgrade_payload = cls(
            architecture=architecture,
            package_bytes=package_bytes,
            package_sha256=package_sha256,
            package_signature=package_signature,
            package_url=package_url,
            package_version=package_version,
            rollback=rollback,
            schema_version=schema_version,
            source_package_bytes=source_package_bytes,
            source_package_url=source_package_url,
            target_binary_digest=target_binary_digest,
            target_build_digest=target_build_digest,
        )

        return agent_upgrade_payload
