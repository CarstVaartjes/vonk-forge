from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.package_rollback_source import PackageRollbackSource





T = TypeVar("T", bound="AgentPackageSource")



@_attrs_define
class AgentPackageSource:
    """
        Attributes:
            architecture (Literal['linux-arm64']):
            build_digest (str):
            package (PackageRollbackSource):
            package_bytes (int):
            package_url (str):
            schema_version (Literal[2]):
     """

    architecture: Literal['linux-arm64']
    build_digest: str
    package: PackageRollbackSource
    package_bytes: int
    package_url: str
    schema_version: Literal[2]





    def to_dict(self) -> dict[str, Any]:
        from ..models.package_rollback_source import PackageRollbackSource # noqa: PLC0415
        architecture = self.architecture

        build_digest = self.build_digest

        package = self.package.to_dict()

        package_bytes = self.package_bytes

        package_url = self.package_url

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "architecture": architecture,
            "build_digest": build_digest,
            "package": package,
            "package_bytes": package_bytes,
            "package_url": package_url,
            "schema_version": schema_version,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.package_rollback_source import PackageRollbackSource # noqa: PLC0415
        d = dict(src_dict)
        architecture = cast(Literal['linux-arm64'] , d.pop("architecture"))
        if architecture != 'linux-arm64':
            raise ValueError(f"architecture must match const 'linux-arm64', got '{architecture}'")

        build_digest = d.pop("build_digest")

        package = PackageRollbackSource.from_dict(d.pop("package"))




        package_bytes = d.pop("package_bytes")

        package_url = d.pop("package_url")

        schema_version = cast(Literal[2] , d.pop("schema_version"))
        if schema_version != 2:
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        agent_package_source = cls(
            architecture=architecture,
            build_digest=build_digest,
            package=package,
            package_bytes=package_bytes,
            package_url=package_url,
            schema_version=schema_version,
        )

        return agent_package_source
