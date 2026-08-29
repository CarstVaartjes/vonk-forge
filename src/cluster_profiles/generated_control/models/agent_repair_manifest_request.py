from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.agent_upgrade_package_request import AgentUpgradePackageRequest





T = TypeVar("T", bound="AgentRepairManifestRequest")



@_attrs_define
class AgentRepairManifestRequest:
    """
        Attributes:
            authority_sha256 (str):
            kind (Literal['agent-upgrade-repair']):
            node_id (str):
            package (AgentUpgradePackageRequest):
            schema_version (Literal[1]):
     """

    authority_sha256: str
    kind: Literal['agent-upgrade-repair']
    node_id: str
    package: 'AgentUpgradePackageRequest'
    schema_version: Literal[1]





    def to_dict(self) -> dict[str, Any]:
        from ..models.agent_upgrade_package_request import AgentUpgradePackageRequest
        authority_sha256 = self.authority_sha256

        kind = self.kind

        node_id = self.node_id

        package = self.package.to_dict()

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "authority_sha256": authority_sha256,
            "kind": kind,
            "node_id": node_id,
            "package": package,
            "schema_version": schema_version,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.agent_upgrade_package_request import AgentUpgradePackageRequest
        d = dict(src_dict)
        authority_sha256 = d.pop("authority_sha256")

        kind = cast(Literal['agent-upgrade-repair'] , d.pop("kind"))
        if kind != 'agent-upgrade-repair':
            raise ValueError(f"kind must match const 'agent-upgrade-repair', got '{kind}'")

        node_id = d.pop("node_id")

        package = AgentUpgradePackageRequest.from_dict(d.pop("package"))




        schema_version = cast(Literal[1] , d.pop("schema_version"))
        if schema_version != 1:
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        agent_repair_manifest_request = cls(
            authority_sha256=authority_sha256,
            kind=kind,
            node_id=node_id,
            package=package,
            schema_version=schema_version,
        )

        return agent_repair_manifest_request
