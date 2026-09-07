from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import Literal, Union, cast
import datetime

if TYPE_CHECKING:
  from ..models.agent_deployment_evidence import AgentDeploymentEvidence
  from ..models.recipe_library_evidence import RecipeLibraryEvidence
  from ..models.platform_boundary import PlatformBoundary
  from ..models.workload_provenance import WorkloadProvenance





T = TypeVar("T", bound="DeploymentProvenance")



@_attrs_define
class DeploymentProvenance:
    """
        Attributes:
            agents (list['AgentDeploymentEvidence']):
            generated_at (datetime.datetime):
            platform (list['PlatformBoundary']):
            recipe_library (RecipeLibraryEvidence):
            workloads (list['WorkloadProvenance']):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    agents: list['AgentDeploymentEvidence']
    generated_at: datetime.datetime
    platform: list['PlatformBoundary']
    recipe_library: 'RecipeLibraryEvidence'
    workloads: list['WorkloadProvenance']
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.agent_deployment_evidence import AgentDeploymentEvidence
        from ..models.recipe_library_evidence import RecipeLibraryEvidence
        from ..models.platform_boundary import PlatformBoundary
        from ..models.workload_provenance import WorkloadProvenance
        agents = []
        for agents_item_data in self.agents:
            agents_item = agents_item_data.to_dict()
            agents.append(agents_item)



        generated_at = self.generated_at.isoformat()

        platform = []
        for platform_item_data in self.platform:
            platform_item = platform_item_data.to_dict()
            platform.append(platform_item)



        recipe_library = self.recipe_library.to_dict()

        workloads = []
        for workloads_item_data in self.workloads:
            workloads_item = workloads_item_data.to_dict()
            workloads.append(workloads_item)



        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "agents": agents,
            "generated_at": generated_at,
            "platform": platform,
            "recipe_library": recipe_library,
            "workloads": workloads,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.agent_deployment_evidence import AgentDeploymentEvidence
        from ..models.recipe_library_evidence import RecipeLibraryEvidence
        from ..models.platform_boundary import PlatformBoundary
        from ..models.workload_provenance import WorkloadProvenance
        d = dict(src_dict)
        agents = []
        _agents = d.pop("agents")
        for agents_item_data in (_agents):
            agents_item = AgentDeploymentEvidence.from_dict(agents_item_data)



            agents.append(agents_item)


        generated_at = isoparse(d.pop("generated_at"))




        platform = []
        _platform = d.pop("platform")
        for platform_item_data in (_platform):
            platform_item = PlatformBoundary.from_dict(platform_item_data)



            platform.append(platform_item)


        recipe_library = RecipeLibraryEvidence.from_dict(d.pop("recipe_library"))




        workloads = []
        _workloads = d.pop("workloads")
        for workloads_item_data in (_workloads):
            workloads_item = WorkloadProvenance.from_dict(workloads_item_data)



            workloads.append(workloads_item)


        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        deployment_provenance = cls(
            agents=agents,
            generated_at=generated_at,
            platform=platform,
            recipe_library=recipe_library,
            workloads=workloads,
            schema_version=schema_version,
        )

        return deployment_provenance
