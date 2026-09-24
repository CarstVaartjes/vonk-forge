from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.fleet_profile_installation_effect import FleetProfileInstallationEffect
  from ..models.fleet_profile_run_effect import FleetProfileRunEffect
  from ..models.fleet_profile_pending_effect import FleetProfilePendingEffect





T = TypeVar("T", bound="FleetProfileEffects")



@_attrs_define
class FleetProfileEffects:
    """ Identified live effects, including complete distributed membership.

        Attributes:
            installations (list['FleetProfileInstallationEffect']):
            runs (list['FleetProfileRunEffect']):
            superseded (list['FleetProfilePendingEffect']):
     """

    installations: list['FleetProfileInstallationEffect']
    runs: list['FleetProfileRunEffect']
    superseded: list['FleetProfilePendingEffect']





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_installation_effect import FleetProfileInstallationEffect
        from ..models.fleet_profile_run_effect import FleetProfileRunEffect
        from ..models.fleet_profile_pending_effect import FleetProfilePendingEffect
        installations = []
        for installations_item_data in self.installations:
            installations_item = installations_item_data.to_dict()
            installations.append(installations_item)



        runs = []
        for runs_item_data in self.runs:
            runs_item = runs_item_data.to_dict()
            runs.append(runs_item)



        superseded = []
        for superseded_item_data in self.superseded:
            superseded_item = superseded_item_data.to_dict()
            superseded.append(superseded_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "installations": installations,
            "runs": runs,
            "superseded": superseded,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_installation_effect import FleetProfileInstallationEffect
        from ..models.fleet_profile_run_effect import FleetProfileRunEffect
        from ..models.fleet_profile_pending_effect import FleetProfilePendingEffect
        d = dict(src_dict)
        installations = []
        _installations = d.pop("installations")
        for installations_item_data in (_installations):
            installations_item = FleetProfileInstallationEffect.from_dict(installations_item_data)



            installations.append(installations_item)


        runs = []
        _runs = d.pop("runs")
        for runs_item_data in (_runs):
            runs_item = FleetProfileRunEffect.from_dict(runs_item_data)



            runs.append(runs_item)


        superseded = []
        _superseded = d.pop("superseded")
        for superseded_item_data in (_superseded):
            superseded_item = FleetProfilePendingEffect.from_dict(superseded_item_data)



            superseded.append(superseded_item)


        fleet_profile_effects = cls(
            installations=installations,
            runs=runs,
            superseded=superseded,
        )

        return fleet_profile_effects
