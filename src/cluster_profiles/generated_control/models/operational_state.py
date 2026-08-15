from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.operational_run import OperationalRun
  from ..models.operational_mapping import OperationalMapping
  from ..models.operational_installation import OperationalInstallation
  from ..models.operational_build import OperationalBuild





T = TypeVar("T", bound="OperationalState")



@_attrs_define
class OperationalState:
    """
        Attributes:
            builds (list['OperationalBuild']):
            installations (list['OperationalInstallation']):
            mappings (list['OperationalMapping']):
            runs (list['OperationalRun']):
     """

    builds: list['OperationalBuild']
    installations: list['OperationalInstallation']
    mappings: list['OperationalMapping']
    runs: list['OperationalRun']





    def to_dict(self) -> dict[str, Any]:
        from ..models.operational_run import OperationalRun
        from ..models.operational_mapping import OperationalMapping
        from ..models.operational_installation import OperationalInstallation
        from ..models.operational_build import OperationalBuild
        builds = []
        for builds_item_data in self.builds:
            builds_item = builds_item_data.to_dict()
            builds.append(builds_item)



        installations = []
        for installations_item_data in self.installations:
            installations_item = installations_item_data.to_dict()
            installations.append(installations_item)



        mappings = []
        for mappings_item_data in self.mappings:
            mappings_item = mappings_item_data.to_dict()
            mappings.append(mappings_item)



        runs = []
        for runs_item_data in self.runs:
            runs_item = runs_item_data.to_dict()
            runs.append(runs_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "builds": builds,
            "installations": installations,
            "mappings": mappings,
            "runs": runs,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.operational_run import OperationalRun
        from ..models.operational_mapping import OperationalMapping
        from ..models.operational_installation import OperationalInstallation
        from ..models.operational_build import OperationalBuild
        d = dict(src_dict)
        builds = []
        _builds = d.pop("builds")
        for builds_item_data in (_builds):
            builds_item = OperationalBuild.from_dict(builds_item_data)



            builds.append(builds_item)


        installations = []
        _installations = d.pop("installations")
        for installations_item_data in (_installations):
            installations_item = OperationalInstallation.from_dict(installations_item_data)



            installations.append(installations_item)


        mappings = []
        _mappings = d.pop("mappings")
        for mappings_item_data in (_mappings):
            mappings_item = OperationalMapping.from_dict(mappings_item_data)



            mappings.append(mappings_item)


        runs = []
        _runs = d.pop("runs")
        for runs_item_data in (_runs):
            runs_item = OperationalRun.from_dict(runs_item_data)



            runs.append(runs_item)


        operational_state = cls(
            builds=builds,
            installations=installations,
            mappings=mappings,
            runs=runs,
        )

        return operational_state
