from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_application_view_state import check_fleet_profile_application_view_state
from ..models.fleet_profile_application_view_state import FleetProfileApplicationViewState
from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast
import datetime

if TYPE_CHECKING:
  from ..models.fleet_profile_application_view_progress import FleetProfileApplicationViewProgress
  from ..models.fleet_profile_application_view_result_type_0 import FleetProfileApplicationViewResultType0





T = TypeVar("T", bound="FleetProfileApplicationView")



@_attrs_define
class FleetProfileApplicationView:
    """
        Attributes:
            created_at (datetime.datetime):
            current_operation_id (Union[None, str]):
            current_step (int):
            id (str):
            plan_digest (str):
            profile_digest (str):
            profile_id (str):
            progress (FleetProfileApplicationViewProgress):
            result (Union['FleetProfileApplicationViewResultType0', None]):
            state (FleetProfileApplicationViewState):
            status_reason (Union[None, str]):
            total_steps (int):
            updated_at (datetime.datetime):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    created_at: datetime.datetime
    current_operation_id: Union[None, str]
    current_step: int
    id: str
    plan_digest: str
    profile_digest: str
    profile_id: str
    progress: 'FleetProfileApplicationViewProgress'
    result: Union['FleetProfileApplicationViewResultType0', None]
    state: FleetProfileApplicationViewState
    status_reason: Union[None, str]
    total_steps: int
    updated_at: datetime.datetime
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_application_view_progress import FleetProfileApplicationViewProgress
        from ..models.fleet_profile_application_view_result_type_0 import FleetProfileApplicationViewResultType0
        created_at = self.created_at.isoformat()

        current_operation_id: Union[None, str]
        current_operation_id = self.current_operation_id

        current_step = self.current_step

        id = self.id

        plan_digest = self.plan_digest

        profile_digest = self.profile_digest

        profile_id = self.profile_id

        progress = self.progress.to_dict()

        result: Union[None, dict[str, Any]]
        if isinstance(self.result, FleetProfileApplicationViewResultType0):
            result = self.result.to_dict()
        else:
            result = self.result

        state: str = self.state

        status_reason: Union[None, str]
        status_reason = self.status_reason

        total_steps = self.total_steps

        updated_at = self.updated_at.isoformat()

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "created_at": created_at,
            "current_operation_id": current_operation_id,
            "current_step": current_step,
            "id": id,
            "plan_digest": plan_digest,
            "profile_digest": profile_digest,
            "profile_id": profile_id,
            "progress": progress,
            "result": result,
            "state": state,
            "status_reason": status_reason,
            "total_steps": total_steps,
            "updated_at": updated_at,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_application_view_progress import FleetProfileApplicationViewProgress
        from ..models.fleet_profile_application_view_result_type_0 import FleetProfileApplicationViewResultType0
        d = dict(src_dict)
        created_at = isoparse(d.pop("created_at"))




        def _parse_current_operation_id(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        current_operation_id = _parse_current_operation_id(d.pop("current_operation_id"))


        current_step = d.pop("current_step")

        id = d.pop("id")

        plan_digest = d.pop("plan_digest")

        profile_digest = d.pop("profile_digest")

        profile_id = d.pop("profile_id")

        progress = FleetProfileApplicationViewProgress.from_dict(d.pop("progress"))




        def _parse_result(data: object) -> Union['FleetProfileApplicationViewResultType0', None]:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_0 = FleetProfileApplicationViewResultType0.from_dict(data)



                return result_type_0
            except: # noqa: E722
                pass
            return cast(Union['FleetProfileApplicationViewResultType0', None], data)

        result = _parse_result(d.pop("result"))


        state = check_fleet_profile_application_view_state(d.pop("state"))




        def _parse_status_reason(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        status_reason = _parse_status_reason(d.pop("status_reason"))


        total_steps = d.pop("total_steps")

        updated_at = isoparse(d.pop("updated_at"))




        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        fleet_profile_application_view = cls(
            created_at=created_at,
            current_operation_id=current_operation_id,
            current_step=current_step,
            id=id,
            plan_digest=plan_digest,
            profile_digest=profile_digest,
            profile_id=profile_id,
            progress=progress,
            result=result,
            state=state,
            status_reason=status_reason,
            total_steps=total_steps,
            updated_at=updated_at,
            schema_version=schema_version,
        )

        return fleet_profile_application_view
