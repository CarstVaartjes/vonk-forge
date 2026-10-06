from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.agent_upgrade_package import AgentUpgradePackage
  from ..models.agent_upgrade_repair_manifest import AgentUpgradeRepairManifest
  from ..models.agent_upgrade_request_intent import AgentUpgradeRequestIntent
  from ..models.agent_upgrade_rollout_payload_sources import AgentUpgradeRolloutPayloadSources





T = TypeVar("T", bound="AgentUpgradeRolloutPayload")



@_attrs_define
class AgentUpgradeRolloutPayload:
    """ An upgrade rollout: the package, the order, and each Spark's rollback source.

        Attributes:
            node_order (list[str]):
            package (AgentUpgradePackage): The signed package a rollout installs on every Spark it targets.
            request_intent (AgentUpgradeRequestIntent): Which Sparks the operator asked for: all of them, or an explicit
                list.
            sources (AgentUpgradeRolloutPayloadSources):
            repair_manifest (AgentUpgradeRepairManifest | None | Unset):
     """

    node_order: list[str]
    package: AgentUpgradePackage
    request_intent: AgentUpgradeRequestIntent
    sources: AgentUpgradeRolloutPayloadSources
    repair_manifest: AgentUpgradeRepairManifest | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.agent_upgrade_package import AgentUpgradePackage # noqa: PLC0415
        from ..models.agent_upgrade_repair_manifest import AgentUpgradeRepairManifest # noqa: PLC0415
        from ..models.agent_upgrade_request_intent import AgentUpgradeRequestIntent # noqa: PLC0415
        from ..models.agent_upgrade_rollout_payload_sources import AgentUpgradeRolloutPayloadSources # noqa: PLC0415
        node_order = self.node_order



        package = self.package.to_dict()

        request_intent = self.request_intent.to_dict()

        sources = self.sources.to_dict()

        repair_manifest: dict[str, Any] | None | Unset
        if isinstance(self.repair_manifest, Unset):
            repair_manifest = UNSET
        elif isinstance(self.repair_manifest, AgentUpgradeRepairManifest):
            repair_manifest = self.repair_manifest.to_dict()
        else:
            repair_manifest = self.repair_manifest


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "node_order": node_order,
            "package": package,
            "request_intent": request_intent,
            "sources": sources,
        })
        if repair_manifest is not UNSET:
            field_dict["repair_manifest"] = repair_manifest

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.agent_upgrade_package import AgentUpgradePackage # noqa: PLC0415
        from ..models.agent_upgrade_repair_manifest import AgentUpgradeRepairManifest # noqa: PLC0415
        from ..models.agent_upgrade_request_intent import AgentUpgradeRequestIntent # noqa: PLC0415
        from ..models.agent_upgrade_rollout_payload_sources import AgentUpgradeRolloutPayloadSources # noqa: PLC0415
        d = dict(src_dict)
        node_order = cast(list[str], d.pop("node_order"))


        package = AgentUpgradePackage.from_dict(d.pop("package"))




        request_intent = AgentUpgradeRequestIntent.from_dict(d.pop("request_intent"))




        sources = AgentUpgradeRolloutPayloadSources.from_dict(d.pop("sources"))




        def _parse_repair_manifest(data: object) -> AgentUpgradeRepairManifest | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                repair_manifest_type_0 = AgentUpgradeRepairManifest.from_dict(data)



                return repair_manifest_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AgentUpgradeRepairManifest | None | Unset, data)

        repair_manifest = _parse_repair_manifest(d.pop("repair_manifest", UNSET))


        agent_upgrade_rollout_payload = cls(
            node_order=node_order,
            package=package,
            request_intent=request_intent,
            sources=sources,
            repair_manifest=repair_manifest,
        )

        return agent_upgrade_rollout_payload
