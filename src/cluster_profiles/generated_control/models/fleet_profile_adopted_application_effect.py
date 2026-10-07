from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.fleet_profile_adopted_stop_effect import FleetProfileAdoptedStopEffect





T = TypeVar("T", bound="FleetProfileAdoptedApplicationEffect")



@_attrs_define
class FleetProfileAdoptedApplicationEffect:
    """ An exact continuing executor authorized by the newer reviewed snapshot.

        Attributes:
            application_id (str):
            node_ids (list[str]):
            plan_digest (str):
            workload_intent_ordinal (int):
            assignment_ids (list[str] | Unset):
            stops (list[FleetProfileAdoptedStopEffect] | Unset):
     """

    application_id: str
    node_ids: list[str]
    plan_digest: str
    workload_intent_ordinal: int
    assignment_ids: list[str] | Unset = UNSET
    stops: list[FleetProfileAdoptedStopEffect] | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_adopted_stop_effect import FleetProfileAdoptedStopEffect # noqa: PLC0415
        application_id = self.application_id

        node_ids = self.node_ids



        plan_digest = self.plan_digest

        workload_intent_ordinal = self.workload_intent_ordinal

        assignment_ids: list[str] | Unset = UNSET
        if not isinstance(self.assignment_ids, Unset):
            assignment_ids = self.assignment_ids



        stops: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.stops, Unset):
            stops = []
            for stops_item_data in self.stops:
                stops_item = stops_item_data.to_dict()
                stops.append(stops_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "application_id": application_id,
            "node_ids": node_ids,
            "plan_digest": plan_digest,
            "workload_intent_ordinal": workload_intent_ordinal,
        })
        if assignment_ids is not UNSET:
            field_dict["assignment_ids"] = assignment_ids
        if stops is not UNSET:
            field_dict["stops"] = stops

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_adopted_stop_effect import FleetProfileAdoptedStopEffect # noqa: PLC0415
        d = dict(src_dict)
        application_id = d.pop("application_id")

        node_ids = cast(list[str], d.pop("node_ids"))


        plan_digest = d.pop("plan_digest")

        workload_intent_ordinal = d.pop("workload_intent_ordinal")

        assignment_ids = cast(list[str], d.pop("assignment_ids", UNSET))


        _stops = d.pop("stops", UNSET)
        stops: list[FleetProfileAdoptedStopEffect] | Unset = UNSET
        if _stops is not UNSET:
            stops = []
            for stops_item_data in _stops:
                stops_item = FleetProfileAdoptedStopEffect.from_dict(stops_item_data)



                stops.append(stops_item)


        fleet_profile_adopted_application_effect = cls(
            application_id=application_id,
            node_ids=node_ids,
            plan_digest=plan_digest,
            workload_intent_ordinal=workload_intent_ordinal,
            assignment_ids=assignment_ids,
            stops=stops,
        )

        return fleet_profile_adopted_application_effect
