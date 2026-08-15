from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.plan_reason import PlanReason
  from ..models.stop_node_impact_response import StopNodeImpactResponse





T = TypeVar("T", bound="StopPlanResponse")



@_attrs_define
class StopPlanResponse:
    """
        Attributes:
            alias (str):
            allowed (bool):
            authority_digest (str):
            blockers (list['PlanReason']):
            installation_id (str):
            nodes (list['StopNodeImpactResponse']):
            plan_digest (str):
            recipe_revision_id (str):
            route_state (str):
            route_withdrawal (bool):
            run_id (str):
            run_state (str):
            total_active_memory_reservation_bytes (int):
            warnings (list['PlanReason']):
            route_digest (Union[None, Unset, str]):
            route_generation (Union[None, Unset, int]):
     """

    alias: str
    allowed: bool
    authority_digest: str
    blockers: list['PlanReason']
    installation_id: str
    nodes: list['StopNodeImpactResponse']
    plan_digest: str
    recipe_revision_id: str
    route_state: str
    route_withdrawal: bool
    run_id: str
    run_state: str
    total_active_memory_reservation_bytes: int
    warnings: list['PlanReason']
    route_digest: Union[None, Unset, str] = UNSET
    route_generation: Union[None, Unset, int] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.plan_reason import PlanReason
        from ..models.stop_node_impact_response import StopNodeImpactResponse
        alias = self.alias

        allowed = self.allowed

        authority_digest = self.authority_digest

        blockers = []
        for blockers_item_data in self.blockers:
            blockers_item = blockers_item_data.to_dict()
            blockers.append(blockers_item)



        installation_id = self.installation_id

        nodes = []
        for nodes_item_data in self.nodes:
            nodes_item = nodes_item_data.to_dict()
            nodes.append(nodes_item)



        plan_digest = self.plan_digest

        recipe_revision_id = self.recipe_revision_id

        route_state = self.route_state

        route_withdrawal = self.route_withdrawal

        run_id = self.run_id

        run_state = self.run_state

        total_active_memory_reservation_bytes = self.total_active_memory_reservation_bytes

        warnings = []
        for warnings_item_data in self.warnings:
            warnings_item = warnings_item_data.to_dict()
            warnings.append(warnings_item)



        route_digest: Union[None, Unset, str]
        if isinstance(self.route_digest, Unset):
            route_digest = UNSET
        else:
            route_digest = self.route_digest

        route_generation: Union[None, Unset, int]
        if isinstance(self.route_generation, Unset):
            route_generation = UNSET
        else:
            route_generation = self.route_generation


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "alias": alias,
            "allowed": allowed,
            "authority_digest": authority_digest,
            "blockers": blockers,
            "installation_id": installation_id,
            "nodes": nodes,
            "plan_digest": plan_digest,
            "recipe_revision_id": recipe_revision_id,
            "route_state": route_state,
            "route_withdrawal": route_withdrawal,
            "run_id": run_id,
            "run_state": run_state,
            "total_active_memory_reservation_bytes": total_active_memory_reservation_bytes,
            "warnings": warnings,
        })
        if route_digest is not UNSET:
            field_dict["route_digest"] = route_digest
        if route_generation is not UNSET:
            field_dict["route_generation"] = route_generation

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.plan_reason import PlanReason
        from ..models.stop_node_impact_response import StopNodeImpactResponse
        d = dict(src_dict)
        alias = d.pop("alias")

        allowed = d.pop("allowed")

        authority_digest = d.pop("authority_digest")

        blockers = []
        _blockers = d.pop("blockers")
        for blockers_item_data in (_blockers):
            blockers_item = PlanReason.from_dict(blockers_item_data)



            blockers.append(blockers_item)


        installation_id = d.pop("installation_id")

        nodes = []
        _nodes = d.pop("nodes")
        for nodes_item_data in (_nodes):
            nodes_item = StopNodeImpactResponse.from_dict(nodes_item_data)



            nodes.append(nodes_item)


        plan_digest = d.pop("plan_digest")

        recipe_revision_id = d.pop("recipe_revision_id")

        route_state = d.pop("route_state")

        route_withdrawal = d.pop("route_withdrawal")

        run_id = d.pop("run_id")

        run_state = d.pop("run_state")

        total_active_memory_reservation_bytes = d.pop("total_active_memory_reservation_bytes")

        warnings = []
        _warnings = d.pop("warnings")
        for warnings_item_data in (_warnings):
            warnings_item = PlanReason.from_dict(warnings_item_data)



            warnings.append(warnings_item)


        def _parse_route_digest(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        route_digest = _parse_route_digest(d.pop("route_digest", UNSET))


        def _parse_route_generation(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        route_generation = _parse_route_generation(d.pop("route_generation", UNSET))


        stop_plan_response = cls(
            alias=alias,
            allowed=allowed,
            authority_digest=authority_digest,
            blockers=blockers,
            installation_id=installation_id,
            nodes=nodes,
            plan_digest=plan_digest,
            recipe_revision_id=recipe_revision_id,
            route_state=route_state,
            route_withdrawal=route_withdrawal,
            run_id=run_id,
            run_state=run_state,
            total_active_memory_reservation_bytes=total_active_memory_reservation_bytes,
            warnings=warnings,
            route_digest=route_digest,
            route_generation=route_generation,
        )

        return stop_plan_response
