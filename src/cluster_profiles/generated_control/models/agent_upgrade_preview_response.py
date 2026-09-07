from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.agent_upgrade_preview_response_strategy import AgentUpgradePreviewResponseStrategy
from ..models.agent_upgrade_preview_response_strategy import check_agent_upgrade_preview_response_strategy
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.agent_repair_manifest_request import AgentRepairManifestRequest
  from ..models.agent_upgrade_package_request import AgentUpgradePackageRequest





T = TypeVar("T", bound="AgentUpgradePreviewResponse")



@_attrs_define
class AgentUpgradePreviewResponse:
    """
        Attributes:
            authority_revision (str):
            node_ids (list[str]):
            package (AgentUpgradePackageRequest):
            plan_digest (str):
            strategy (AgentUpgradePreviewResponseStrategy):
            repair_manifest (Union['AgentRepairManifestRequest', None, Unset]):
     """

    authority_revision: str
    node_ids: list[str]
    package: 'AgentUpgradePackageRequest'
    plan_digest: str
    strategy: AgentUpgradePreviewResponseStrategy
    repair_manifest: Union['AgentRepairManifestRequest', None, Unset] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.agent_repair_manifest_request import AgentRepairManifestRequest
        from ..models.agent_upgrade_package_request import AgentUpgradePackageRequest
        authority_revision = self.authority_revision

        node_ids = self.node_ids



        package = self.package.to_dict()

        plan_digest = self.plan_digest

        strategy: str = self.strategy

        repair_manifest: Union[None, Unset, dict[str, Any]]
        if isinstance(self.repair_manifest, Unset):
            repair_manifest = UNSET
        elif isinstance(self.repair_manifest, AgentRepairManifestRequest):
            repair_manifest = self.repair_manifest.to_dict()
        else:
            repair_manifest = self.repair_manifest


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "authority_revision": authority_revision,
            "node_ids": node_ids,
            "package": package,
            "plan_digest": plan_digest,
            "strategy": strategy,
        })
        if repair_manifest is not UNSET:
            field_dict["repair_manifest"] = repair_manifest

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.agent_repair_manifest_request import AgentRepairManifestRequest
        from ..models.agent_upgrade_package_request import AgentUpgradePackageRequest
        d = dict(src_dict)
        authority_revision = d.pop("authority_revision")

        node_ids = cast(list[str], d.pop("node_ids"))


        package = AgentUpgradePackageRequest.from_dict(d.pop("package"))




        plan_digest = d.pop("plan_digest")

        strategy = check_agent_upgrade_preview_response_strategy(d.pop("strategy"))




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


        agent_upgrade_preview_response = cls(
            authority_revision=authority_revision,
            node_ids=node_ids,
            package=package,
            plan_digest=plan_digest,
            strategy=strategy,
            repair_manifest=repair_manifest,
        )

        return agent_upgrade_preview_response
