from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.agent_upgrade_apply_request_strategy import AgentUpgradeApplyRequestStrategy
from ..models.agent_upgrade_apply_request_strategy import check_agent_upgrade_apply_request_strategy
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.agent_repair_manifest_request import AgentRepairManifestRequest
  from ..models.agent_upgrade_package_request import AgentUpgradePackageRequest





T = TypeVar("T", bound="AgentUpgradeApplyRequest")



@_attrs_define
class AgentUpgradeApplyRequest:
    """
        Attributes:
            plan_digest (str):
            node_ids (Union[None, Unset, list[str]]):
            package (Union['AgentUpgradePackageRequest', None, Unset]):
            repair_manifest (Union['AgentRepairManifestRequest', None, Unset]):
            strategy (Union[Unset, AgentUpgradeApplyRequestStrategy]):  Default: 'one-at-a-time'.
     """

    plan_digest: str
    node_ids: Union[None, Unset, list[str]] = UNSET
    package: Union['AgentUpgradePackageRequest', None, Unset] = UNSET
    repair_manifest: Union['AgentRepairManifestRequest', None, Unset] = UNSET
    strategy: Union[Unset, AgentUpgradeApplyRequestStrategy] = 'one-at-a-time'





    def to_dict(self) -> dict[str, Any]:
        from ..models.agent_repair_manifest_request import AgentRepairManifestRequest
        from ..models.agent_upgrade_package_request import AgentUpgradePackageRequest
        plan_digest = self.plan_digest

        node_ids: Union[None, Unset, list[str]]
        if isinstance(self.node_ids, Unset):
            node_ids = UNSET
        elif isinstance(self.node_ids, list):
            node_ids = self.node_ids


        else:
            node_ids = self.node_ids

        package: Union[None, Unset, dict[str, Any]]
        if isinstance(self.package, Unset):
            package = UNSET
        elif isinstance(self.package, AgentUpgradePackageRequest):
            package = self.package.to_dict()
        else:
            package = self.package

        repair_manifest: Union[None, Unset, dict[str, Any]]
        if isinstance(self.repair_manifest, Unset):
            repair_manifest = UNSET
        elif isinstance(self.repair_manifest, AgentRepairManifestRequest):
            repair_manifest = self.repair_manifest.to_dict()
        else:
            repair_manifest = self.repair_manifest

        strategy: Union[Unset, str] = UNSET
        if not isinstance(self.strategy, Unset):
            strategy = self.strategy



        field_dict: dict[str, Any] = {}

        field_dict.update({
            "plan_digest": plan_digest,
        })
        if node_ids is not UNSET:
            field_dict["node_ids"] = node_ids
        if package is not UNSET:
            field_dict["package"] = package
        if repair_manifest is not UNSET:
            field_dict["repair_manifest"] = repair_manifest
        if strategy is not UNSET:
            field_dict["strategy"] = strategy

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.agent_repair_manifest_request import AgentRepairManifestRequest
        from ..models.agent_upgrade_package_request import AgentUpgradePackageRequest
        d = dict(src_dict)
        plan_digest = d.pop("plan_digest")

        def _parse_node_ids(data: object) -> Union[None, Unset, list[str]]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                node_ids_type_0 = cast(list[str], data)

                return node_ids_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, Unset, list[str]], data)

        node_ids = _parse_node_ids(d.pop("node_ids", UNSET))


        def _parse_package(data: object) -> Union['AgentUpgradePackageRequest', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                package_type_0 = AgentUpgradePackageRequest.from_dict(data)



                return package_type_0
            except: # noqa: E722
                pass
            return cast(Union['AgentUpgradePackageRequest', None, Unset], data)

        package = _parse_package(d.pop("package", UNSET))


        def _parse_repair_manifest(data: object) -> Union['AgentRepairManifestRequest', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                repair_manifest_type_0 = AgentRepairManifestRequest.from_dict(data)



                return repair_manifest_type_0
            except: # noqa: E722
                pass
            return cast(Union['AgentRepairManifestRequest', None, Unset], data)

        repair_manifest = _parse_repair_manifest(d.pop("repair_manifest", UNSET))


        _strategy = d.pop("strategy", UNSET)
        strategy: Union[Unset, AgentUpgradeApplyRequestStrategy]
        if isinstance(_strategy,  Unset):
            strategy = UNSET
        else:
            strategy = check_agent_upgrade_apply_request_strategy(_strategy)




        agent_upgrade_apply_request = cls(
            plan_digest=plan_digest,
            node_ids=node_ids,
            package=package,
            repair_manifest=repair_manifest,
            strategy=strategy,
        )

        return agent_upgrade_apply_request
