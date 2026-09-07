from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import cast, Union

if TYPE_CHECKING:
  from ..models.recipe_operation_stopped_result import RecipeOperationStoppedResult
  from ..models.recipe_operation_cancellation_result import RecipeOperationCancellationResult
  from ..models.recipe_operation_activated_result import RecipeOperationActivatedResult
  from ..models.recipe_operation_result import RecipeOperationResult
  from ..models.recipe_job_run_result import RecipeJobRunResult
  from ..models.recipe_operation_progress_result import RecipeOperationProgressResult





T = TypeVar("T", bound="OperationResponse")



@_attrs_define
class OperationResponse:
    """
        Attributes:
            id (str):
            kind (str):
            nodes (list[str]):
            owner_id (str):
            plan_digest (str):
            result (Union['RecipeJobRunResult', 'RecipeOperationActivatedResult', 'RecipeOperationCancellationResult',
                'RecipeOperationProgressResult', 'RecipeOperationResult', 'RecipeOperationStoppedResult', None]):
            state (str):
     """

    id: str
    kind: str
    nodes: list[str]
    owner_id: str
    plan_digest: str
    result: Union['RecipeJobRunResult', 'RecipeOperationActivatedResult', 'RecipeOperationCancellationResult', 'RecipeOperationProgressResult', 'RecipeOperationResult', 'RecipeOperationStoppedResult', None]
    state: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_operation_stopped_result import RecipeOperationStoppedResult
        from ..models.recipe_operation_cancellation_result import RecipeOperationCancellationResult
        from ..models.recipe_operation_activated_result import RecipeOperationActivatedResult
        from ..models.recipe_operation_result import RecipeOperationResult
        from ..models.recipe_job_run_result import RecipeJobRunResult
        from ..models.recipe_operation_progress_result import RecipeOperationProgressResult
        id = self.id

        kind = self.kind

        nodes = self.nodes



        owner_id = self.owner_id

        plan_digest = self.plan_digest

        result: Union[None, dict[str, Any]]
        if isinstance(self.result, RecipeOperationResult):
            result = self.result.to_dict()
        elif isinstance(self.result, RecipeOperationProgressResult):
            result = self.result.to_dict()
        elif isinstance(self.result, RecipeOperationCancellationResult):
            result = self.result.to_dict()
        elif isinstance(self.result, RecipeOperationActivatedResult):
            result = self.result.to_dict()
        elif isinstance(self.result, RecipeOperationStoppedResult):
            result = self.result.to_dict()
        elif isinstance(self.result, RecipeJobRunResult):
            result = self.result.to_dict()
        else:
            result = self.result

        state = self.state


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "id": id,
            "kind": kind,
            "nodes": nodes,
            "owner_id": owner_id,
            "plan_digest": plan_digest,
            "result": result,
            "state": state,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_operation_stopped_result import RecipeOperationStoppedResult
        from ..models.recipe_operation_cancellation_result import RecipeOperationCancellationResult
        from ..models.recipe_operation_activated_result import RecipeOperationActivatedResult
        from ..models.recipe_operation_result import RecipeOperationResult
        from ..models.recipe_job_run_result import RecipeJobRunResult
        from ..models.recipe_operation_progress_result import RecipeOperationProgressResult
        d = dict(src_dict)
        id = d.pop("id")

        kind = d.pop("kind")

        nodes = cast(list[str], d.pop("nodes"))


        owner_id = d.pop("owner_id")

        plan_digest = d.pop("plan_digest")

        def _parse_result(data: object) -> Union['RecipeJobRunResult', 'RecipeOperationActivatedResult', 'RecipeOperationCancellationResult', 'RecipeOperationProgressResult', 'RecipeOperationResult', 'RecipeOperationStoppedResult', None]:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_0 = RecipeOperationResult.from_dict(data)



                return result_type_0
            except: # noqa: E722
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_1 = RecipeOperationProgressResult.from_dict(data)



                return result_type_1
            except: # noqa: E722
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_2 = RecipeOperationCancellationResult.from_dict(data)



                return result_type_2
            except: # noqa: E722
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_3 = RecipeOperationActivatedResult.from_dict(data)



                return result_type_3
            except: # noqa: E722
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_4 = RecipeOperationStoppedResult.from_dict(data)



                return result_type_4
            except: # noqa: E722
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_5 = RecipeJobRunResult.from_dict(data)



                return result_type_5
            except: # noqa: E722
                pass
            return cast(Union['RecipeJobRunResult', 'RecipeOperationActivatedResult', 'RecipeOperationCancellationResult', 'RecipeOperationProgressResult', 'RecipeOperationResult', 'RecipeOperationStoppedResult', None], data)

        result = _parse_result(d.pop("result"))


        state = d.pop("state")

        operation_response = cls(
            id=id,
            kind=kind,
            nodes=nodes,
            owner_id=owner_id,
            plan_digest=plan_digest,
            result=result,
            state=state,
        )

        return operation_response
