from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.model_cache_operator_response_action import check_model_cache_operator_response_action
from ..models.model_cache_operator_response_action import ModelCacheOperatorResponseAction
from ..models.model_cache_operator_response_state_type_0 import check_model_cache_operator_response_state_type_0
from ..models.model_cache_operator_response_state_type_0 import ModelCacheOperatorResponseStateType0
from ..models.model_cache_operator_response_state_type_1 import check_model_cache_operator_response_state_type_1
from ..models.model_cache_operator_response_state_type_1 import ModelCacheOperatorResponseStateType1
from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.availability_operation_failure import AvailabilityOperationFailure
  from ..models.model_cache_cancellation import ModelCacheCancellation
  from ..models.model_cache_download_result import ModelCacheDownloadResult
  from ..models.model_cache_removal_result import ModelCacheRemovalResult
  from ..models.operation_blocker import OperationBlocker
  from ..models.operation_progress import OperationProgress





T = TypeVar("T", bound="ModelCacheOperatorResponse")



@_attrs_define
class ModelCacheOperatorResponse:
    """ CLI-shaped result without exposing an internal plan/digest workflow.

        Attributes:
            action (ModelCacheOperatorResponseAction):
            phase (str):
            progress (OperationProgress): Canonical durable progress payload shared by Controller and agents.
            request_key (str):
            selector (str):
            state (ModelCacheOperatorResponseStateType0 | ModelCacheOperatorResponseStateType1):
            transferred_bytes (int):
            blockers (list[OperationBlocker] | Unset):
            cancellation (ModelCacheCancellation | None | Unset):
            cancelled_operations (list[str] | Unset):
            eta_seconds (float | None | Unset):
            failure (AvailabilityOperationFailure | None | Unset):
            model_content_sha256 (None | str | Unset):
            next_actions (list[str] | Unset):
            next_attempt_at (None | str | Unset):
            operation_id (None | str | Unset):
            preserved (list[str] | Unset):
            result (ModelCacheDownloadResult | ModelCacheRemovalResult | None | Unset):
            total_bytes (int | None | Unset):
     """

    action: ModelCacheOperatorResponseAction
    phase: str
    progress: OperationProgress
    request_key: str
    selector: str
    state: ModelCacheOperatorResponseStateType0 | ModelCacheOperatorResponseStateType1
    transferred_bytes: int
    blockers: list[OperationBlocker] | Unset = UNSET
    cancellation: ModelCacheCancellation | None | Unset = UNSET
    cancelled_operations: list[str] | Unset = UNSET
    eta_seconds: float | None | Unset = UNSET
    failure: AvailabilityOperationFailure | None | Unset = UNSET
    model_content_sha256: None | str | Unset = UNSET
    next_actions: list[str] | Unset = UNSET
    next_attempt_at: None | str | Unset = UNSET
    operation_id: None | str | Unset = UNSET
    preserved: list[str] | Unset = UNSET
    result: ModelCacheDownloadResult | ModelCacheRemovalResult | None | Unset = UNSET
    total_bytes: int | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.availability_operation_failure import AvailabilityOperationFailure # noqa: PLC0415
        from ..models.model_cache_cancellation import ModelCacheCancellation # noqa: PLC0415
        from ..models.model_cache_download_result import ModelCacheDownloadResult # noqa: PLC0415
        from ..models.model_cache_removal_result import ModelCacheRemovalResult # noqa: PLC0415
        from ..models.operation_blocker import OperationBlocker # noqa: PLC0415
        from ..models.operation_progress import OperationProgress # noqa: PLC0415
        action: str = self.action

        phase = self.phase

        progress = self.progress.to_dict()

        request_key = self.request_key

        selector = self.selector

        state: str
        if isinstance(self.state, str):
            state = self.state
        else:
            state = self.state


        transferred_bytes = self.transferred_bytes

        blockers: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.blockers, Unset):
            blockers = []
            for blockers_item_data in self.blockers:
                blockers_item = blockers_item_data.to_dict()
                blockers.append(blockers_item)



        cancellation: dict[str, Any] | None | Unset
        if isinstance(self.cancellation, Unset):
            cancellation = UNSET
        elif isinstance(self.cancellation, ModelCacheCancellation):
            cancellation = self.cancellation.to_dict()
        else:
            cancellation = self.cancellation

        cancelled_operations: list[str] | Unset = UNSET
        if not isinstance(self.cancelled_operations, Unset):
            cancelled_operations = self.cancelled_operations



        eta_seconds: float | None | Unset
        if isinstance(self.eta_seconds, Unset):
            eta_seconds = UNSET
        else:
            eta_seconds = self.eta_seconds

        failure: dict[str, Any] | None | Unset
        if isinstance(self.failure, Unset):
            failure = UNSET
        elif isinstance(self.failure, AvailabilityOperationFailure):
            failure = self.failure.to_dict()
        else:
            failure = self.failure

        model_content_sha256: None | str | Unset
        if isinstance(self.model_content_sha256, Unset):
            model_content_sha256 = UNSET
        else:
            model_content_sha256 = self.model_content_sha256

        next_actions: list[str] | Unset = UNSET
        if not isinstance(self.next_actions, Unset):
            next_actions = self.next_actions



        next_attempt_at: None | str | Unset
        if isinstance(self.next_attempt_at, Unset):
            next_attempt_at = UNSET
        else:
            next_attempt_at = self.next_attempt_at

        operation_id: None | str | Unset
        if isinstance(self.operation_id, Unset):
            operation_id = UNSET
        else:
            operation_id = self.operation_id

        preserved: list[str] | Unset = UNSET
        if not isinstance(self.preserved, Unset):
            preserved = self.preserved



        result: dict[str, Any] | None | Unset
        if isinstance(self.result, Unset):
            result = UNSET
        elif isinstance(self.result, ModelCacheDownloadResult):
            result = self.result.to_dict()
        elif isinstance(self.result, ModelCacheRemovalResult):
            result = self.result.to_dict()
        else:
            result = self.result

        total_bytes: int | None | Unset
        if isinstance(self.total_bytes, Unset):
            total_bytes = UNSET
        else:
            total_bytes = self.total_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "action": action,
            "phase": phase,
            "progress": progress,
            "request_key": request_key,
            "selector": selector,
            "state": state,
            "transferred_bytes": transferred_bytes,
        })
        if blockers is not UNSET:
            field_dict["blockers"] = blockers
        if cancellation is not UNSET:
            field_dict["cancellation"] = cancellation
        if cancelled_operations is not UNSET:
            field_dict["cancelled_operations"] = cancelled_operations
        if eta_seconds is not UNSET:
            field_dict["eta_seconds"] = eta_seconds
        if failure is not UNSET:
            field_dict["failure"] = failure
        if model_content_sha256 is not UNSET:
            field_dict["model_content_sha256"] = model_content_sha256
        if next_actions is not UNSET:
            field_dict["next_actions"] = next_actions
        if next_attempt_at is not UNSET:
            field_dict["next_attempt_at"] = next_attempt_at
        if operation_id is not UNSET:
            field_dict["operation_id"] = operation_id
        if preserved is not UNSET:
            field_dict["preserved"] = preserved
        if result is not UNSET:
            field_dict["result"] = result
        if total_bytes is not UNSET:
            field_dict["total_bytes"] = total_bytes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.availability_operation_failure import AvailabilityOperationFailure # noqa: PLC0415
        from ..models.model_cache_cancellation import ModelCacheCancellation # noqa: PLC0415
        from ..models.model_cache_download_result import ModelCacheDownloadResult # noqa: PLC0415
        from ..models.model_cache_removal_result import ModelCacheRemovalResult # noqa: PLC0415
        from ..models.operation_blocker import OperationBlocker # noqa: PLC0415
        from ..models.operation_progress import OperationProgress # noqa: PLC0415
        d = dict(src_dict)
        action = check_model_cache_operator_response_action(d.pop("action"))




        phase = d.pop("phase")

        progress = OperationProgress.from_dict(d.pop("progress"))




        request_key = d.pop("request_key")

        selector = d.pop("selector")

        def _parse_state(data: object) -> ModelCacheOperatorResponseStateType0 | ModelCacheOperatorResponseStateType1:
            try:
                if not isinstance(data, str):
                    raise TypeError()
                state_type_0 = check_model_cache_operator_response_state_type_0(data)



                return state_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            if not isinstance(data, str):
                raise TypeError()
            state_type_1 = check_model_cache_operator_response_state_type_1(data)



            return state_type_1

        state = _parse_state(d.pop("state"))


        transferred_bytes = d.pop("transferred_bytes")

        _blockers = d.pop("blockers", UNSET)
        blockers: list[OperationBlocker] | Unset = UNSET
        if _blockers is not UNSET:
            blockers = []
            for blockers_item_data in _blockers:
                blockers_item = OperationBlocker.from_dict(blockers_item_data)



                blockers.append(blockers_item)


        def _parse_cancellation(data: object) -> ModelCacheCancellation | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                cancellation_type_0 = ModelCacheCancellation.from_dict(data)



                return cancellation_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ModelCacheCancellation | None | Unset, data)

        cancellation = _parse_cancellation(d.pop("cancellation", UNSET))


        cancelled_operations = cast(list[str], d.pop("cancelled_operations", UNSET))


        def _parse_eta_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        eta_seconds = _parse_eta_seconds(d.pop("eta_seconds", UNSET))


        def _parse_failure(data: object) -> AvailabilityOperationFailure | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                failure_type_0 = AvailabilityOperationFailure.from_dict(data)



                return failure_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AvailabilityOperationFailure | None | Unset, data)

        failure = _parse_failure(d.pop("failure", UNSET))


        def _parse_model_content_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        model_content_sha256 = _parse_model_content_sha256(d.pop("model_content_sha256", UNSET))


        next_actions = cast(list[str], d.pop("next_actions", UNSET))


        def _parse_next_attempt_at(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        next_attempt_at = _parse_next_attempt_at(d.pop("next_attempt_at", UNSET))


        def _parse_operation_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        operation_id = _parse_operation_id(d.pop("operation_id", UNSET))


        preserved = cast(list[str], d.pop("preserved", UNSET))


        def _parse_result(data: object) -> ModelCacheDownloadResult | ModelCacheRemovalResult | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_0 = ModelCacheDownloadResult.from_dict(data)



                return result_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_1 = ModelCacheRemovalResult.from_dict(data)



                return result_type_1
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ModelCacheDownloadResult | ModelCacheRemovalResult | None | Unset, data)

        result = _parse_result(d.pop("result", UNSET))


        def _parse_total_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        total_bytes = _parse_total_bytes(d.pop("total_bytes", UNSET))


        model_cache_operator_response = cls(
            action=action,
            phase=phase,
            progress=progress,
            request_key=request_key,
            selector=selector,
            state=state,
            transferred_bytes=transferred_bytes,
            blockers=blockers,
            cancellation=cancellation,
            cancelled_operations=cancelled_operations,
            eta_seconds=eta_seconds,
            failure=failure,
            model_content_sha256=model_content_sha256,
            next_actions=next_actions,
            next_attempt_at=next_attempt_at,
            operation_id=operation_id,
            preserved=preserved,
            result=result,
            total_bytes=total_bytes,
        )

        return model_cache_operator_response
