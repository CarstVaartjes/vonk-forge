from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_application_view_state import check_fleet_profile_application_view_state
from ..models.fleet_profile_application_view_state import FleetProfileApplicationViewState
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast
import datetime

if TYPE_CHECKING:
  from ..models.fleet_profile_application_cancellation_view import FleetProfileApplicationCancellationView
  from ..models.fleet_profile_application_progress import FleetProfileApplicationProgress
  from ..models.fleet_profile_application_result import FleetProfileApplicationResult





T = TypeVar("T", bound="FleetProfileApplicationView")



@_attrs_define
class FleetProfileApplicationView:
    """
        Attributes:
            created_at (datetime.datetime):
            current_operation_id (None | str):
            current_step (int):
            id (str):
            plan_digest (str):
            profile_digest (str):
            profile_id (str):
            progress (FleetProfileApplicationProgress): Typed progress tree persisted with every profile application.
            request_key (str):
            result (FleetProfileApplicationResult | None):
            state (FleetProfileApplicationViewState):
            status_reason (None | str):
            total_steps (int):
            updated_at (datetime.datetime):
            attempt (int | Unset):  Default: 1.
            cancellation (FleetProfileApplicationCancellationView | None | Unset):
            retry_of_application_id (None | str | Unset):
            schema_version (Literal[2] | Unset):  Default: 2.
     """

    created_at: datetime.datetime
    current_operation_id: None | str
    current_step: int
    id: str
    plan_digest: str
    profile_digest: str
    profile_id: str
    progress: FleetProfileApplicationProgress
    request_key: str
    result: FleetProfileApplicationResult | None
    state: FleetProfileApplicationViewState
    status_reason: None | str
    total_steps: int
    updated_at: datetime.datetime
    attempt: int | Unset = 1
    cancellation: FleetProfileApplicationCancellationView | None | Unset = UNSET
    retry_of_application_id: None | str | Unset = UNSET
    schema_version: Literal[2] | Unset = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_application_cancellation_view import FleetProfileApplicationCancellationView # noqa: PLC0415
        from ..models.fleet_profile_application_progress import FleetProfileApplicationProgress # noqa: PLC0415
        from ..models.fleet_profile_application_result import FleetProfileApplicationResult # noqa: PLC0415
        created_at = self.created_at.isoformat()

        current_operation_id: None | str
        current_operation_id = self.current_operation_id

        current_step = self.current_step

        id = self.id

        plan_digest = self.plan_digest

        profile_digest = self.profile_digest

        profile_id = self.profile_id

        progress = self.progress.to_dict()

        request_key = self.request_key

        result: dict[str, Any] | None
        if isinstance(self.result, FleetProfileApplicationResult):
            result = self.result.to_dict()
        else:
            result = self.result

        state: str = self.state

        status_reason: None | str
        status_reason = self.status_reason

        total_steps = self.total_steps

        updated_at = self.updated_at.isoformat()

        attempt = self.attempt

        cancellation: dict[str, Any] | None | Unset
        if isinstance(self.cancellation, Unset):
            cancellation = UNSET
        elif isinstance(self.cancellation, FleetProfileApplicationCancellationView):
            cancellation = self.cancellation.to_dict()
        else:
            cancellation = self.cancellation

        retry_of_application_id: None | str | Unset
        if isinstance(self.retry_of_application_id, Unset):
            retry_of_application_id = UNSET
        else:
            retry_of_application_id = self.retry_of_application_id

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
            "request_key": request_key,
            "result": result,
            "state": state,
            "status_reason": status_reason,
            "total_steps": total_steps,
            "updated_at": updated_at,
        })
        if attempt is not UNSET:
            field_dict["attempt"] = attempt
        if cancellation is not UNSET:
            field_dict["cancellation"] = cancellation
        if retry_of_application_id is not UNSET:
            field_dict["retry_of_application_id"] = retry_of_application_id
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_application_cancellation_view import FleetProfileApplicationCancellationView # noqa: PLC0415
        from ..models.fleet_profile_application_progress import FleetProfileApplicationProgress # noqa: PLC0415
        from ..models.fleet_profile_application_result import FleetProfileApplicationResult # noqa: PLC0415
        d = dict(src_dict)
        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))




        def _parse_current_operation_id(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        current_operation_id = _parse_current_operation_id(d.pop("current_operation_id"))


        current_step = d.pop("current_step")

        id = d.pop("id")

        plan_digest = d.pop("plan_digest")

        profile_digest = d.pop("profile_digest")

        profile_id = d.pop("profile_id")

        progress = FleetProfileApplicationProgress.from_dict(d.pop("progress"))




        request_key = d.pop("request_key")

        def _parse_result(data: object) -> FleetProfileApplicationResult | None:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_0 = FleetProfileApplicationResult.from_dict(data)



                return result_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(FleetProfileApplicationResult | None, data)

        result = _parse_result(d.pop("result"))


        state = check_fleet_profile_application_view_state(d.pop("state"))




        def _parse_status_reason(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        status_reason = _parse_status_reason(d.pop("status_reason"))


        total_steps = d.pop("total_steps")

        updated_at = datetime.datetime.fromisoformat(d.pop("updated_at"))




        attempt = d.pop("attempt", UNSET)

        def _parse_cancellation(data: object) -> FleetProfileApplicationCancellationView | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                cancellation_type_0 = FleetProfileApplicationCancellationView.from_dict(data)



                return cancellation_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(FleetProfileApplicationCancellationView | None | Unset, data)

        cancellation = _parse_cancellation(d.pop("cancellation", UNSET))


        def _parse_retry_of_application_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        retry_of_application_id = _parse_retry_of_application_id(d.pop("retry_of_application_id", UNSET))


        schema_version = cast(Literal[2] | Unset , d.pop("schema_version", UNSET))
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
            request_key=request_key,
            result=result,
            state=state,
            status_reason=status_reason,
            total_steps=total_steps,
            updated_at=updated_at,
            attempt=attempt,
            cancellation=cancellation,
            retry_of_application_id=retry_of_application_id,
            schema_version=schema_version,
        )

        return fleet_profile_application_view
