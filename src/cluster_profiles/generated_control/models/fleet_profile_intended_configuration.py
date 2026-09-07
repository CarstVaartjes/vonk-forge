from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_intended_configuration_installation_policy import check_fleet_profile_intended_configuration_installation_policy
from ..models.fleet_profile_intended_configuration_installation_policy import FleetProfileIntendedConfigurationInstallationPolicy
from typing import cast

if TYPE_CHECKING:
  from ..models.fleet_profile_scope import FleetProfileScope
  from ..models.fleet_profile_assignment import FleetProfileAssignment





T = TypeVar("T", bound="FleetProfileIntendedConfiguration")



@_attrs_define
class FleetProfileIntendedConfiguration:
    """ Immutable desired configuration captured when execution is admitted.

        Attributes:
            assignments (list['FleetProfileAssignment']):
            installation_policy (FleetProfileIntendedConfigurationInstallationPolicy):
            profile_digest (str):
            scope (FleetProfileScope): The complete set of Sparks reconciled by a profile.

                Scope is deliberately independent from assignments.  A member with no
                assignment is an intentional idle outcome when the profile is applied.
     """

    assignments: list['FleetProfileAssignment']
    installation_policy: FleetProfileIntendedConfigurationInstallationPolicy
    profile_digest: str
    scope: 'FleetProfileScope'





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_scope import FleetProfileScope
        from ..models.fleet_profile_assignment import FleetProfileAssignment
        assignments = []
        for assignments_item_data in self.assignments:
            assignments_item = assignments_item_data.to_dict()
            assignments.append(assignments_item)



        installation_policy: str = self.installation_policy

        profile_digest = self.profile_digest

        scope = self.scope.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "assignments": assignments,
            "installation_policy": installation_policy,
            "profile_digest": profile_digest,
            "scope": scope,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_scope import FleetProfileScope
        from ..models.fleet_profile_assignment import FleetProfileAssignment
        d = dict(src_dict)
        assignments = []
        _assignments = d.pop("assignments")
        for assignments_item_data in (_assignments):
            assignments_item = FleetProfileAssignment.from_dict(assignments_item_data)



            assignments.append(assignments_item)


        installation_policy = check_fleet_profile_intended_configuration_installation_policy(d.pop("installation_policy"))




        profile_digest = d.pop("profile_digest")

        scope = FleetProfileScope.from_dict(d.pop("scope"))




        fleet_profile_intended_configuration = cls(
            assignments=assignments,
            installation_policy=installation_policy,
            profile_digest=profile_digest,
            scope=scope,
        )

        return fleet_profile_intended_configuration
