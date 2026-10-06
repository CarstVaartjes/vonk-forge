from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="FleetProfileAdoptedApplicationEffect")



@_attrs_define
class FleetProfileAdoptedApplicationEffect:
    """ An exact continuing executor authorized by the newer reviewed snapshot.

        Attributes:
            application_id (str):
            assignment_ids (list[str]):
            node_ids (list[str]):
            plan_digest (str):
            workload_intent_ordinal (int):
     """

    application_id: str
    assignment_ids: list[str]
    node_ids: list[str]
    plan_digest: str
    workload_intent_ordinal: int





    def to_dict(self) -> dict[str, Any]:
        application_id = self.application_id

        assignment_ids = self.assignment_ids



        node_ids = self.node_ids



        plan_digest = self.plan_digest

        workload_intent_ordinal = self.workload_intent_ordinal


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "application_id": application_id,
            "assignment_ids": assignment_ids,
            "node_ids": node_ids,
            "plan_digest": plan_digest,
            "workload_intent_ordinal": workload_intent_ordinal,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        application_id = d.pop("application_id")

        assignment_ids = cast(list[str], d.pop("assignment_ids"))


        node_ids = cast(list[str], d.pop("node_ids"))


        plan_digest = d.pop("plan_digest")

        workload_intent_ordinal = d.pop("workload_intent_ordinal")

        fleet_profile_adopted_application_effect = cls(
            application_id=application_id,
            assignment_ids=assignment_ids,
            node_ids=node_ids,
            plan_digest=plan_digest,
            workload_intent_ordinal=workload_intent_ordinal,
        )

        return fleet_profile_adopted_application_effect
