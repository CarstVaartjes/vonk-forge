from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.model_cache_operator_response_action import check_model_cache_operator_response_action
from ..models.model_cache_operator_response_action import ModelCacheOperatorResponseAction
from ..models.model_cache_operator_response_state import check_model_cache_operator_response_state
from ..models.model_cache_operator_response_state import ModelCacheOperatorResponseState
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.availability_operation_failure import AvailabilityOperationFailure
  from ..models.model_cache_removal_result import ModelCacheRemovalResult
  from ..models.model_cache_cancellation import ModelCacheCancellation
  from ..models.operation_progress import OperationProgress
  from ..models.model_cache_download_result import ModelCacheDownloadResult





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
            state (ModelCacheOperatorResponseState):
            transferred_bytes (int):
            cancellation (Union['ModelCacheCancellation', None, Unset]):
            cancelled_operations (Union[Unset, list[str]]):
            eta_seconds (Union[None, Unset, float]):
            failure (Union['AvailabilityOperationFailure', None, Unset]):
            model_content_sha256 (Union[None, Unset, str]):
            next_actions (Union[Unset, list[str]]):
            operation_id (Union[None, Unset, str]):
            preserved (Union[Unset, list[str]]):
            result (Union['ModelCacheDownloadResult', 'ModelCacheRemovalResult', None, Unset]):
            review_digest (Union[None, Unset, str]):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
            total_bytes (Union[None, Unset, int]):
     """

    action: ModelCacheOperatorResponseAction
    phase: str
    progress: 'OperationProgress'
    request_key: str
    selector: str
    state: ModelCacheOperatorResponseState
    transferred_bytes: int
    cancellation: Union['ModelCacheCancellation', None, Unset] = UNSET
    cancelled_operations: Union[Unset, list[str]] = UNSET
    eta_seconds: Union[None, Unset, float] = UNSET
    failure: Union['AvailabilityOperationFailure', None, Unset] = UNSET
    model_content_sha256: Union[None, Unset, str] = UNSET
    next_actions: Union[Unset, list[str]] = UNSET
    operation_id: Union[None, Unset, str] = UNSET
    preserved: Union[Unset, list[str]] = UNSET
    result: Union['ModelCacheDownloadResult', 'ModelCacheRemovalResult', None, Unset] = UNSET
    review_digest: Union[None, Unset, str] = UNSET
    schema_version: Union[Literal[2], Unset] = 2
    total_bytes: Union[None, Unset, int] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.availability_operation_failure import AvailabilityOperationFailure
        from ..models.model_cache_removal_result import ModelCacheRemovalResult
        from ..models.model_cache_cancellation import ModelCacheCancellation
        from ..models.operation_progress import OperationProgress
        from ..models.model_cache_download_result import ModelCacheDownloadResult
        action: str = self.action

        phase = self.phase

        progress = self.progress.to_dict()

        request_key = self.request_key

        selector = self.selector

        state: str = self.state

        transferred_bytes = self.transferred_bytes

        cancellation: Union[None, Unset, dict[str, Any]]
        if isinstance(self.cancellation, Unset):
            cancellation = UNSET
        elif isinstance(self.cancellation, ModelCacheCancellation):
            cancellation = self.cancellation.to_dict()
        else:
            cancellation = self.cancellation

        cancelled_operations: Union[Unset, list[str]] = UNSET
        if not isinstance(self.cancelled_operations, Unset):
            cancelled_operations = self.cancelled_operations



        eta_seconds: Union[None, Unset, float]
        if isinstance(self.eta_seconds, Unset):
            eta_seconds = UNSET
        else:
            eta_seconds = self.eta_seconds

        failure: Union[None, Unset, dict[str, Any]]
        if isinstance(self.failure, Unset):
            failure = UNSET
        elif isinstance(self.failure, AvailabilityOperationFailure):
            failure = self.failure.to_dict()
        else:
            failure = self.failure

        model_content_sha256: Union[None, Unset, str]
        if isinstance(self.model_content_sha256, Unset):
            model_content_sha256 = UNSET
        else:
            model_content_sha256 = self.model_content_sha256

        next_actions: Union[Unset, list[str]] = UNSET
        if not isinstance(self.next_actions, Unset):
            next_actions = self.next_actions



        operation_id: Union[None, Unset, str]
        if isinstance(self.operation_id, Unset):
            operation_id = UNSET
        else:
            operation_id = self.operation_id

        preserved: Union[Unset, list[str]] = UNSET
        if not isinstance(self.preserved, Unset):
            preserved = self.preserved



        result: Union[None, Unset, dict[str, Any]]
        if isinstance(self.result, Unset):
            result = UNSET
        elif isinstance(self.result, ModelCacheDownloadResult):
            result = self.result.to_dict()
        elif isinstance(self.result, ModelCacheRemovalResult):
            result = self.result.to_dict()
        else:
            result = self.result

        review_digest: Union[None, Unset, str]
        if isinstance(self.review_digest, Unset):
            review_digest = UNSET
        else:
            review_digest = self.review_digest

        schema_version = self.schema_version

        total_bytes: Union[None, Unset, int]
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
        if operation_id is not UNSET:
            field_dict["operation_id"] = operation_id
        if preserved is not UNSET:
            field_dict["preserved"] = preserved
        if result is not UNSET:
            field_dict["result"] = result
        if review_digest is not UNSET:
            field_dict["review_digest"] = review_digest
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if total_bytes is not UNSET:
            field_dict["total_bytes"] = total_bytes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.availability_operation_failure import AvailabilityOperationFailure
        from ..models.model_cache_removal_result import ModelCacheRemovalResult
        from ..models.model_cache_cancellation import ModelCacheCancellation
        from ..models.operation_progress import OperationProgress
        from ..models.model_cache_download_result import ModelCacheDownloadResult
        d = dict(src_dict)
        action = check_model_cache_operator_response_action(d.pop("action"))




        phase = d.pop("phase")

        progress = OperationProgress.from_dict(d.pop("progress"))




        request_key = d.pop("request_key")

        selector = d.pop("selector")

        state = check_model_cache_operator_response_state(d.pop("state"))




        transferred_bytes = d.pop("transferred_bytes")

        def _parse_cancellation(data: object) -> Union['ModelCacheCancellation', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                cancellation_type_0 = ModelCacheCancellation.from_dict(data)



                return cancellation_type_0
            except: # noqa: E722
                pass
            return cast(Union['ModelCacheCancellation', None, Unset], data)

        cancellation = _parse_cancellation(d.pop("cancellation", UNSET))


        cancelled_operations = cast(list[str], d.pop("cancelled_operations", UNSET))


        def _parse_eta_seconds(data: object) -> Union[None, Unset, float]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, float], data)

        eta_seconds = _parse_eta_seconds(d.pop("eta_seconds", UNSET))


        def _parse_failure(data: object) -> Union['AvailabilityOperationFailure', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                failure_type_0 = AvailabilityOperationFailure.from_dict(data)



                return failure_type_0
            except: # noqa: E722
                pass
            return cast(Union['AvailabilityOperationFailure', None, Unset], data)

        failure = _parse_failure(d.pop("failure", UNSET))


        def _parse_model_content_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        model_content_sha256 = _parse_model_content_sha256(d.pop("model_content_sha256", UNSET))


        next_actions = cast(list[str], d.pop("next_actions", UNSET))


        def _parse_operation_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        operation_id = _parse_operation_id(d.pop("operation_id", UNSET))


        preserved = cast(list[str], d.pop("preserved", UNSET))


        def _parse_result(data: object) -> Union['ModelCacheDownloadResult', 'ModelCacheRemovalResult', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_0 = ModelCacheDownloadResult.from_dict(data)



                return result_type_0
            except: # noqa: E722
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_1 = ModelCacheRemovalResult.from_dict(data)



                return result_type_1
            except: # noqa: E722
                pass
            return cast(Union['ModelCacheDownloadResult', 'ModelCacheRemovalResult', None, Unset], data)

        result = _parse_result(d.pop("result", UNSET))


        def _parse_review_digest(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        review_digest = _parse_review_digest(d.pop("review_digest", UNSET))


        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        def _parse_total_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        total_bytes = _parse_total_bytes(d.pop("total_bytes", UNSET))


        model_cache_operator_response = cls(
            action=action,
            phase=phase,
            progress=progress,
            request_key=request_key,
            selector=selector,
            state=state,
            transferred_bytes=transferred_bytes,
            cancellation=cancellation,
            cancelled_operations=cancelled_operations,
            eta_seconds=eta_seconds,
            failure=failure,
            model_content_sha256=model_content_sha256,
            next_actions=next_actions,
            operation_id=operation_id,
            preserved=preserved,
            result=result,
            review_digest=review_digest,
            schema_version=schema_version,
            total_bytes=total_bytes,
        )

        return model_cache_operator_response
