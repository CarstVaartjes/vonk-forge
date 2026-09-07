from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.model_cache_operation_response_kind import check_model_cache_operation_response_kind
from ..models.model_cache_operation_response_kind import ModelCacheOperationResponseKind
from ..models.model_cache_operation_response_state import check_model_cache_operation_response_state
from ..models.model_cache_operation_response_state import ModelCacheOperationResponseState
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.availability_operation_failure import AvailabilityOperationFailure
  from ..models.model_cache_operation_progress import ModelCacheOperationProgress
  from ..models.model_cache_operation_response_result_type_0 import ModelCacheOperationResponseResultType0





T = TypeVar("T", bound="ModelCacheOperationResponse")



@_attrs_define
class ModelCacheOperationResponse:
    """
        Attributes:
            artifact_set_sha256 (Union[None, str]):
            attempt (int):
            completed_at (Union[None, str]):
            created_at (str):
            id (str):
            kind (ModelCacheOperationResponseKind):
            plan_digest (Union[None, str]):
            progress (ModelCacheOperationProgress):
            request_key (str):
            state (ModelCacheOperationResponseState):
            updated_at (str):
            failure (Union['AvailabilityOperationFailure', None, Unset]):
            result (Union['ModelCacheOperationResponseResultType0', None, Unset]):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    artifact_set_sha256: Union[None, str]
    attempt: int
    completed_at: Union[None, str]
    created_at: str
    id: str
    kind: ModelCacheOperationResponseKind
    plan_digest: Union[None, str]
    progress: 'ModelCacheOperationProgress'
    request_key: str
    state: ModelCacheOperationResponseState
    updated_at: str
    failure: Union['AvailabilityOperationFailure', None, Unset] = UNSET
    result: Union['ModelCacheOperationResponseResultType0', None, Unset] = UNSET
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.availability_operation_failure import AvailabilityOperationFailure
        from ..models.model_cache_operation_progress import ModelCacheOperationProgress
        from ..models.model_cache_operation_response_result_type_0 import ModelCacheOperationResponseResultType0
        artifact_set_sha256: Union[None, str]
        artifact_set_sha256 = self.artifact_set_sha256

        attempt = self.attempt

        completed_at: Union[None, str]
        completed_at = self.completed_at

        created_at = self.created_at

        id = self.id

        kind: str = self.kind

        plan_digest: Union[None, str]
        plan_digest = self.plan_digest

        progress = self.progress.to_dict()

        request_key = self.request_key

        state: str = self.state

        updated_at = self.updated_at

        failure: Union[None, Unset, dict[str, Any]]
        if isinstance(self.failure, Unset):
            failure = UNSET
        elif isinstance(self.failure, AvailabilityOperationFailure):
            failure = self.failure.to_dict()
        else:
            failure = self.failure

        result: Union[None, Unset, dict[str, Any]]
        if isinstance(self.result, Unset):
            result = UNSET
        elif isinstance(self.result, ModelCacheOperationResponseResultType0):
            result = self.result.to_dict()
        else:
            result = self.result

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifact_set_sha256": artifact_set_sha256,
            "attempt": attempt,
            "completed_at": completed_at,
            "created_at": created_at,
            "id": id,
            "kind": kind,
            "plan_digest": plan_digest,
            "progress": progress,
            "request_key": request_key,
            "state": state,
            "updated_at": updated_at,
        })
        if failure is not UNSET:
            field_dict["failure"] = failure
        if result is not UNSET:
            field_dict["result"] = result
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.availability_operation_failure import AvailabilityOperationFailure
        from ..models.model_cache_operation_progress import ModelCacheOperationProgress
        from ..models.model_cache_operation_response_result_type_0 import ModelCacheOperationResponseResultType0
        d = dict(src_dict)
        def _parse_artifact_set_sha256(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        artifact_set_sha256 = _parse_artifact_set_sha256(d.pop("artifact_set_sha256"))


        attempt = d.pop("attempt")

        def _parse_completed_at(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        completed_at = _parse_completed_at(d.pop("completed_at"))


        created_at = d.pop("created_at")

        id = d.pop("id")

        kind = check_model_cache_operation_response_kind(d.pop("kind"))




        def _parse_plan_digest(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        plan_digest = _parse_plan_digest(d.pop("plan_digest"))


        progress = ModelCacheOperationProgress.from_dict(d.pop("progress"))




        request_key = d.pop("request_key")

        state = check_model_cache_operation_response_state(d.pop("state"))




        updated_at = d.pop("updated_at")

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


        def _parse_result(data: object) -> Union['ModelCacheOperationResponseResultType0', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_0 = ModelCacheOperationResponseResultType0.from_dict(data)



                return result_type_0
            except: # noqa: E722
                pass
            return cast(Union['ModelCacheOperationResponseResultType0', None, Unset], data)

        result = _parse_result(d.pop("result", UNSET))


        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        model_cache_operation_response = cls(
            artifact_set_sha256=artifact_set_sha256,
            attempt=attempt,
            completed_at=completed_at,
            created_at=created_at,
            id=id,
            kind=kind,
            plan_digest=plan_digest,
            progress=progress,
            request_key=request_key,
            state=state,
            updated_at=updated_at,
            failure=failure,
            result=result,
            schema_version=schema_version,
        )

        return model_cache_operation_response
