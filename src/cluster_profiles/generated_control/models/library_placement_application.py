from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.library_placement_application_desired_state import check_library_placement_application_desired_state
from ..models.library_placement_application_desired_state import LibraryPlacementApplicationDesiredState
from ..models.library_placement_application_state import check_library_placement_application_state
from ..models.library_placement_application_state import LibraryPlacementApplicationState
from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast
from typing import Union
import datetime

if TYPE_CHECKING:
  from ..models.library_placement_locations import LibraryPlacementLocations
  from ..models.fleet_profile_application_progress import FleetProfileApplicationProgress
  from ..models.fleet_profile_application_result import FleetProfileApplicationResult





T = TypeVar("T", bound="LibraryPlacementApplication")



@_attrs_define
class LibraryPlacementApplication:
    """
        Attributes:
            alias (Union[None, str]):
            created_at (datetime.datetime):
            current_operation_id (Union[None, str]):
            current_step (int):
            desired_state (LibraryPlacementApplicationDesiredState):
            id (str):
            locations (LibraryPlacementLocations):
            plan_digest (str):
            progress (FleetProfileApplicationProgress): Typed progress tree persisted with every profile application.
            recipe_id (str):
            recipe_revision_id (str):
            selected_node_ids (list[str]):
            state (LibraryPlacementApplicationState):
            status_reason (Union[None, str]):
            total_steps (int):
            updated_at (datetime.datetime):
            attempt (Union[Unset, int]):  Default: 1.
            result (Union['FleetProfileApplicationResult', None, Unset]):
            retry_of_application_id (Union[None, Unset, str]):
            schema_version (Union[Literal[1], Unset]):  Default: 1.
     """

    alias: Union[None, str]
    created_at: datetime.datetime
    current_operation_id: Union[None, str]
    current_step: int
    desired_state: LibraryPlacementApplicationDesiredState
    id: str
    locations: 'LibraryPlacementLocations'
    plan_digest: str
    progress: 'FleetProfileApplicationProgress'
    recipe_id: str
    recipe_revision_id: str
    selected_node_ids: list[str]
    state: LibraryPlacementApplicationState
    status_reason: Union[None, str]
    total_steps: int
    updated_at: datetime.datetime
    attempt: Union[Unset, int] = 1
    result: Union['FleetProfileApplicationResult', None, Unset] = UNSET
    retry_of_application_id: Union[None, Unset, str] = UNSET
    schema_version: Union[Literal[1], Unset] = 1





    def to_dict(self) -> dict[str, Any]:
        from ..models.library_placement_locations import LibraryPlacementLocations
        from ..models.fleet_profile_application_progress import FleetProfileApplicationProgress
        from ..models.fleet_profile_application_result import FleetProfileApplicationResult
        alias: Union[None, str]
        alias = self.alias

        created_at = self.created_at.isoformat()

        current_operation_id: Union[None, str]
        current_operation_id = self.current_operation_id

        current_step = self.current_step

        desired_state: str = self.desired_state

        id = self.id

        locations = self.locations.to_dict()

        plan_digest = self.plan_digest

        progress = self.progress.to_dict()

        recipe_id = self.recipe_id

        recipe_revision_id = self.recipe_revision_id

        selected_node_ids = self.selected_node_ids



        state: str = self.state

        status_reason: Union[None, str]
        status_reason = self.status_reason

        total_steps = self.total_steps

        updated_at = self.updated_at.isoformat()

        attempt = self.attempt

        result: Union[None, Unset, dict[str, Any]]
        if isinstance(self.result, Unset):
            result = UNSET
        elif isinstance(self.result, FleetProfileApplicationResult):
            result = self.result.to_dict()
        else:
            result = self.result

        retry_of_application_id: Union[None, Unset, str]
        if isinstance(self.retry_of_application_id, Unset):
            retry_of_application_id = UNSET
        else:
            retry_of_application_id = self.retry_of_application_id

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "alias": alias,
            "created_at": created_at,
            "current_operation_id": current_operation_id,
            "current_step": current_step,
            "desired_state": desired_state,
            "id": id,
            "locations": locations,
            "plan_digest": plan_digest,
            "progress": progress,
            "recipe_id": recipe_id,
            "recipe_revision_id": recipe_revision_id,
            "selected_node_ids": selected_node_ids,
            "state": state,
            "status_reason": status_reason,
            "total_steps": total_steps,
            "updated_at": updated_at,
        })
        if attempt is not UNSET:
            field_dict["attempt"] = attempt
        if result is not UNSET:
            field_dict["result"] = result
        if retry_of_application_id is not UNSET:
            field_dict["retry_of_application_id"] = retry_of_application_id
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.library_placement_locations import LibraryPlacementLocations
        from ..models.fleet_profile_application_progress import FleetProfileApplicationProgress
        from ..models.fleet_profile_application_result import FleetProfileApplicationResult
        d = dict(src_dict)
        def _parse_alias(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        alias = _parse_alias(d.pop("alias"))


        created_at = isoparse(d.pop("created_at"))




        def _parse_current_operation_id(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        current_operation_id = _parse_current_operation_id(d.pop("current_operation_id"))


        current_step = d.pop("current_step")

        desired_state = check_library_placement_application_desired_state(d.pop("desired_state"))




        id = d.pop("id")

        locations = LibraryPlacementLocations.from_dict(d.pop("locations"))




        plan_digest = d.pop("plan_digest")

        progress = FleetProfileApplicationProgress.from_dict(d.pop("progress"))




        recipe_id = d.pop("recipe_id")

        recipe_revision_id = d.pop("recipe_revision_id")

        selected_node_ids = cast(list[str], d.pop("selected_node_ids"))


        state = check_library_placement_application_state(d.pop("state"))




        def _parse_status_reason(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        status_reason = _parse_status_reason(d.pop("status_reason"))


        total_steps = d.pop("total_steps")

        updated_at = isoparse(d.pop("updated_at"))




        attempt = d.pop("attempt", UNSET)

        def _parse_result(data: object) -> Union['FleetProfileApplicationResult', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_0 = FleetProfileApplicationResult.from_dict(data)



                return result_type_0
            except: # noqa: E722
                pass
            return cast(Union['FleetProfileApplicationResult', None, Unset], data)

        result = _parse_result(d.pop("result", UNSET))


        def _parse_retry_of_application_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        retry_of_application_id = _parse_retry_of_application_id(d.pop("retry_of_application_id", UNSET))


        schema_version = cast(Union[Literal[1], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 1 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        library_placement_application = cls(
            alias=alias,
            created_at=created_at,
            current_operation_id=current_operation_id,
            current_step=current_step,
            desired_state=desired_state,
            id=id,
            locations=locations,
            plan_digest=plan_digest,
            progress=progress,
            recipe_id=recipe_id,
            recipe_revision_id=recipe_revision_id,
            selected_node_ids=selected_node_ids,
            state=state,
            status_reason=status_reason,
            total_steps=total_steps,
            updated_at=updated_at,
            attempt=attempt,
            result=result,
            retry_of_application_id=retry_of_application_id,
            schema_version=schema_version,
        )

        return library_placement_application
