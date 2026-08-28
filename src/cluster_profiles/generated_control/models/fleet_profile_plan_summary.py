from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="FleetProfilePlanSummary")



@_attrs_define
class FleetProfilePlanSummary:
    """
        Attributes:
            already_correct (int):
            blockers (int):
            distributions (int):
            installs (int):
            placements (int):
            starts (int):
            stops (int):
            uninstalls (int):
     """

    already_correct: int
    blockers: int
    distributions: int
    installs: int
    placements: int
    starts: int
    stops: int
    uninstalls: int





    def to_dict(self) -> dict[str, Any]:
        already_correct = self.already_correct

        blockers = self.blockers

        distributions = self.distributions

        installs = self.installs

        placements = self.placements

        starts = self.starts

        stops = self.stops

        uninstalls = self.uninstalls


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "already_correct": already_correct,
            "blockers": blockers,
            "distributions": distributions,
            "installs": installs,
            "placements": placements,
            "starts": starts,
            "stops": stops,
            "uninstalls": uninstalls,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        already_correct = d.pop("already_correct")

        blockers = d.pop("blockers")

        distributions = d.pop("distributions")

        installs = d.pop("installs")

        placements = d.pop("placements")

        starts = d.pop("starts")

        stops = d.pop("stops")

        uninstalls = d.pop("uninstalls")

        fleet_profile_plan_summary = cls(
            already_correct=already_correct,
            blockers=blockers,
            distributions=distributions,
            installs=installs,
            placements=placements,
            starts=starts,
            stops=stops,
            uninstalls=uninstalls,
        )

        return fleet_profile_plan_summary
