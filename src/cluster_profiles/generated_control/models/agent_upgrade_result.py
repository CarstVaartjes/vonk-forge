from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.package_activation_receipt import PackageActivationReceipt





T = TypeVar("T", bound="AgentUpgradeResult")



@_attrs_define
class AgentUpgradeResult:
    """ Evidence emitted after the Rust agent reports an exact upgrade.

        Attributes:
            activation_receipt (PackageActivationReceipt):
            architecture (Literal['linux-arm64']):
            binary_digest (str):
            build_digest (str):
            package_sha256 (str):
            package_version (str):
            status (Literal['upgraded']):
     """

    activation_receipt: PackageActivationReceipt
    architecture: Literal['linux-arm64']
    binary_digest: str
    build_digest: str
    package_sha256: str
    package_version: str
    status: Literal['upgraded']





    def to_dict(self) -> dict[str, Any]:
        from ..models.package_activation_receipt import PackageActivationReceipt # noqa: PLC0415
        activation_receipt = self.activation_receipt.to_dict()

        architecture = self.architecture

        binary_digest = self.binary_digest

        build_digest = self.build_digest

        package_sha256 = self.package_sha256

        package_version = self.package_version

        status = self.status


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "activation_receipt": activation_receipt,
            "architecture": architecture,
            "binary_digest": binary_digest,
            "build_digest": build_digest,
            "package_sha256": package_sha256,
            "package_version": package_version,
            "status": status,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.package_activation_receipt import PackageActivationReceipt # noqa: PLC0415
        d = dict(src_dict)
        activation_receipt = PackageActivationReceipt.from_dict(d.pop("activation_receipt"))




        architecture = cast(Literal['linux-arm64'] , d.pop("architecture"))
        if architecture != 'linux-arm64':
            raise ValueError(f"architecture must match const 'linux-arm64', got '{architecture}'")

        binary_digest = d.pop("binary_digest")

        build_digest = d.pop("build_digest")

        package_sha256 = d.pop("package_sha256")

        package_version = d.pop("package_version")

        status = cast(Literal['upgraded'] , d.pop("status"))
        if status != 'upgraded':
            raise ValueError(f"status must match const 'upgraded', got '{status}'")

        agent_upgrade_result = cls(
            activation_receipt=activation_receipt,
            architecture=architecture,
            binary_digest=binary_digest,
            build_digest=build_digest,
            package_sha256=package_sha256,
            package_version=package_version,
            status=status,
        )

        return agent_upgrade_result
