from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_application_view_reason_code_type_0 import check_fleet_profile_application_view_reason_code_type_0
from ..models.fleet_profile_application_view_reason_code_type_0 import FleetProfileApplicationViewReasonCodeType0
from ..models.fleet_profile_application_view_state import check_fleet_profile_application_view_state
from ..models.fleet_profile_application_view_state import FleetProfileApplicationViewState
from ..types import UNSET, Unset
from typing import cast
import datetime

if TYPE_CHECKING:
  from ..models.fleet_profile_application_cancellation_view import FleetProfileApplicationCancellationView
  from ..models.fleet_profile_application_progress import FleetProfileApplicationProgress
  from ..models.fleet_profile_application_result import FleetProfileApplicationResult
  from ..models.operation_blocker import OperationBlocker





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
            blockers (list[OperationBlocker] | Unset):
            cancellation (FleetProfileApplicationCancellationView | None | Unset):
            next_attempt_at (datetime.datetime | None | Unset):
            reason_code (FleetProfileApplicationViewReasonCodeType0 | None | Unset):
            retry_of_application_id (None | str | Unset):
            superseded_by (None | str | Unset):
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
    blockers: list[OperationBlocker] | Unset = UNSET
    cancellation: FleetProfileApplicationCancellationView | None | Unset = UNSET
    next_attempt_at: datetime.datetime | None | Unset = UNSET
    reason_code: FleetProfileApplicationViewReasonCodeType0 | None | Unset = UNSET
    retry_of_application_id: None | str | Unset = UNSET
    superseded_by: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_application_cancellation_view import FleetProfileApplicationCancellationView # noqa: PLC0415
        from ..models.fleet_profile_application_progress import FleetProfileApplicationProgress # noqa: PLC0415
        from ..models.fleet_profile_application_result import FleetProfileApplicationResult # noqa: PLC0415
        from ..models.operation_blocker import OperationBlocker # noqa: PLC0415
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

        blockers: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.blockers, Unset):
            blockers = []
            for blockers_item_data in self.blockers:
                blockers_item = blockers_item_data.to_dict()
                blockers.append(blockers_item)



        cancellation: dict[str, Any] | None | Unset
        if isinstance(self.cancellation, Unset):
            cancellation = UNSET
        elif isinstance(self.cancellation, FleetProfileApplicationCancellationView):
            cancellation = self.cancellation.to_dict()
        else:
            cancellation = self.cancellation

        next_attempt_at: None | str | Unset
        if isinstance(self.next_attempt_at, Unset):
            next_attempt_at = UNSET
        elif isinstance(self.next_attempt_at, datetime.datetime):
            next_attempt_at = self.next_attempt_at.isoformat()
        else:
            next_attempt_at = self.next_attempt_at

        reason_code: None | str | Unset
        if isinstance(self.reason_code, Unset):
            reason_code = UNSET
        elif isinstance(self.reason_code, str):
            reason_code = self.reason_code
        else:
            reason_code = self.reason_code

        retry_of_application_id: None | str | Unset
        if isinstance(self.retry_of_application_id, Unset):
            retry_of_application_id = UNSET
        else:
            retry_of_application_id = self.retry_of_application_id

        superseded_by: None | str | Unset
        if isinstance(self.superseded_by, Unset):
            superseded_by = UNSET
        else:
            superseded_by = self.superseded_by


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
        if blockers is not UNSET:
            field_dict["blockers"] = blockers
        if cancellation is not UNSET:
            field_dict["cancellation"] = cancellation
        if next_attempt_at is not UNSET:
            field_dict["next_attempt_at"] = next_attempt_at
        if reason_code is not UNSET:
            field_dict["reason_code"] = reason_code
        if retry_of_application_id is not UNSET:
            field_dict["retry_of_application_id"] = retry_of_application_id
        if superseded_by is not UNSET:
            field_dict["superseded_by"] = superseded_by

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_application_cancellation_view import FleetProfileApplicationCancellationView # noqa: PLC0415
        from ..models.fleet_profile_application_progress import FleetProfileApplicationProgress # noqa: PLC0415
        from ..models.fleet_profile_application_result import FleetProfileApplicationResult # noqa: PLC0415
        from ..models.operation_blocker import OperationBlocker # noqa: PLC0415
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

        _blockers = d.pop("blockers", UNSET)
        blockers: list[OperationBlocker] | Unset = UNSET
        if _blockers is not UNSET:
            blockers = []
            for blockers_item_data in _blockers:
                blockers_item = OperationBlocker.from_dict(blockers_item_data)



                blockers.append(blockers_item)


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


        def _parse_next_attempt_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                next_attempt_at_type_0 = datetime.datetime.fromisoformat(data)



                return next_attempt_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        next_attempt_at = _parse_next_attempt_at(d.pop("next_attempt_at", UNSET))


        def _parse_reason_code(data: object) -> FleetProfileApplicationViewReasonCodeType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                reason_code_type_0 = check_fleet_profile_application_view_reason_code_type_0(data)



                return reason_code_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(FleetProfileApplicationViewReasonCodeType0 | None | Unset, data)

        reason_code = _parse_reason_code(d.pop("reason_code", UNSET))


        def _parse_retry_of_application_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        retry_of_application_id = _parse_retry_of_application_id(d.pop("retry_of_application_id", UNSET))


        def _parse_superseded_by(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        superseded_by = _parse_superseded_by(d.pop("superseded_by", UNSET))


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
            blockers=blockers,
            cancellation=cancellation,
            next_attempt_at=next_attempt_at,
            reason_code=reason_code,
            retry_of_application_id=retry_of_application_id,
            superseded_by=superseded_by,
        )

        return fleet_profile_application_view
