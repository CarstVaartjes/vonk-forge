from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.agent_upgrade_package import AgentUpgradePackage





T = TypeVar("T", bound="AgentUpgradeRepairManifest")



@_attrs_define
class AgentUpgradeRepairManifest:
    """ The repair capsule's authority, bound to one Spark and the package.

        Attributes:
            authority_sha256 (str):
            kind (Literal['agent-upgrade-repair']):
            node_id (str):
            package (AgentUpgradePackage): The signed package a rollout installs on every Spark it targets.
            schema_version (Literal[2]):
     """

    authority_sha256: str
    kind: Literal['agent-upgrade-repair']
    node_id: str
    package: AgentUpgradePackage
    schema_version: Literal[2]





    def to_dict(self) -> dict[str, Any]:
        from ..models.agent_upgrade_package import AgentUpgradePackage # noqa: PLC0415
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
        from ..models.agent_upgrade_package import AgentUpgradePackage # noqa: PLC0415
        d = dict(src_dict)
        authority_sha256 = d.pop("authority_sha256")

        kind = cast(Literal['agent-upgrade-repair'] , d.pop("kind"))
        if kind != 'agent-upgrade-repair':
            raise ValueError(f"kind must match const 'agent-upgrade-repair', got '{kind}'")

        node_id = d.pop("node_id")

        package = AgentUpgradePackage.from_dict(d.pop("package"))




        schema_version = cast(Literal[2] , d.pop("schema_version"))
        if schema_version != 2:
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        agent_upgrade_repair_manifest = cls(
            authority_sha256=authority_sha256,
            kind=kind,
            node_id=node_id,
            package=package,
            schema_version=schema_version,
        )

        return agent_upgrade_repair_manifest
