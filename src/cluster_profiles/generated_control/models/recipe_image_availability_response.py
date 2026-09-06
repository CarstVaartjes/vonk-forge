from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_image_availability_response_state import check_recipe_image_availability_response_state
from ..models.recipe_image_availability_response_state import RecipeImageAvailabilityResponseState
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, cast
from typing import Literal, Union, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.availability_operation_failure import AvailabilityOperationFailure
  from ..models.recipe_image_availability_child import RecipeImageAvailabilityChild
  from ..models.recipe_image_availability_action import RecipeImageAvailabilityAction
  from ..models.operation_progress import OperationProgress
  from ..models.recipe_image_availability_result import RecipeImageAvailabilityResult





T = TypeVar("T", bound="RecipeImageAvailabilityResponse")



@_attrs_define
class RecipeImageAvailabilityResponse:
    """
        Attributes:
            attempt (int):
            created_at (str):
            id (str):
            kind (Literal['recipe.image.availability.v2']):
            progress (OperationProgress): Canonical progress payload persisted on the current operation attempt.
            recipe_content_sha256 (str):
            recipe_revision_id (str):
            request_id (str):
            state (RecipeImageAvailabilityResponseState):
            updated_at (str):
            actions (Union[Unset, list['RecipeImageAvailabilityAction']]):
            children (Union[Unset, list['RecipeImageAvailabilityChild']]):
            failure (Union['AvailabilityOperationFailure', None, Unset]):
            result (Union['RecipeImageAvailabilityResult', None, Unset]):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    attempt: int
    created_at: str
    id: str
    kind: Literal['recipe.image.availability.v2']
    progress: 'OperationProgress'
    recipe_content_sha256: str
    recipe_revision_id: str
    request_id: str
    state: RecipeImageAvailabilityResponseState
    updated_at: str
    actions: Union[Unset, list['RecipeImageAvailabilityAction']] = UNSET
    children: Union[Unset, list['RecipeImageAvailabilityChild']] = UNSET
    failure: Union['AvailabilityOperationFailure', None, Unset] = UNSET
    result: Union['RecipeImageAvailabilityResult', None, Unset] = UNSET
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.availability_operation_failure import AvailabilityOperationFailure
        from ..models.recipe_image_availability_child import RecipeImageAvailabilityChild
        from ..models.recipe_image_availability_action import RecipeImageAvailabilityAction
        from ..models.operation_progress import OperationProgress
        from ..models.recipe_image_availability_result import RecipeImageAvailabilityResult
        attempt = self.attempt

        created_at = self.created_at

        id = self.id

        kind = self.kind

        progress = self.progress.to_dict()

        recipe_content_sha256 = self.recipe_content_sha256

        recipe_revision_id = self.recipe_revision_id

        request_id = self.request_id

        state: str = self.state

        updated_at = self.updated_at

        actions: Union[Unset, list[dict[str, Any]]] = UNSET
        if not isinstance(self.actions, Unset):
            actions = []
            for actions_item_data in self.actions:
                actions_item = actions_item_data.to_dict()
                actions.append(actions_item)



        children: Union[Unset, list[dict[str, Any]]] = UNSET
        if not isinstance(self.children, Unset):
            children = []
            for children_item_data in self.children:
                children_item = children_item_data.to_dict()
                children.append(children_item)



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
        elif isinstance(self.result, RecipeImageAvailabilityResult):
            result = self.result.to_dict()
        else:
            result = self.result

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "attempt": attempt,
            "created_at": created_at,
            "id": id,
            "kind": kind,
            "progress": progress,
            "recipe_content_sha256": recipe_content_sha256,
            "recipe_revision_id": recipe_revision_id,
            "request_id": request_id,
            "state": state,
            "updated_at": updated_at,
        })
        if actions is not UNSET:
            field_dict["actions"] = actions
        if children is not UNSET:
            field_dict["children"] = children
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
        from ..models.recipe_image_availability_child import RecipeImageAvailabilityChild
        from ..models.recipe_image_availability_action import RecipeImageAvailabilityAction
        from ..models.operation_progress import OperationProgress
        from ..models.recipe_image_availability_result import RecipeImageAvailabilityResult
        d = dict(src_dict)
        attempt = d.pop("attempt")

        created_at = d.pop("created_at")

        id = d.pop("id")

        kind = cast(Literal['recipe.image.availability.v2'] , d.pop("kind"))
        if kind != 'recipe.image.availability.v2':
            raise ValueError(f"kind must match const 'recipe.image.availability.v2', got '{kind}'")

        progress = OperationProgress.from_dict(d.pop("progress"))




        recipe_content_sha256 = d.pop("recipe_content_sha256")

        recipe_revision_id = d.pop("recipe_revision_id")

        request_id = d.pop("request_id")

        state = check_recipe_image_availability_response_state(d.pop("state"))




        updated_at = d.pop("updated_at")

        actions = []
        _actions = d.pop("actions", UNSET)
        for actions_item_data in (_actions or []):
            actions_item = RecipeImageAvailabilityAction.from_dict(actions_item_data)



            actions.append(actions_item)


        children = []
        _children = d.pop("children", UNSET)
        for children_item_data in (_children or []):
            children_item = RecipeImageAvailabilityChild.from_dict(children_item_data)



            children.append(children_item)


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


        def _parse_result(data: object) -> Union['RecipeImageAvailabilityResult', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_0 = RecipeImageAvailabilityResult.from_dict(data)



                return result_type_0
            except: # noqa: E722
                pass
            return cast(Union['RecipeImageAvailabilityResult', None, Unset], data)

        result = _parse_result(d.pop("result", UNSET))


        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        recipe_image_availability_response = cls(
            attempt=attempt,
            created_at=created_at,
            id=id,
            kind=kind,
            progress=progress,
            recipe_content_sha256=recipe_content_sha256,
            recipe_revision_id=recipe_revision_id,
            request_id=request_id,
            state=state,
            updated_at=updated_at,
            actions=actions,
            children=children,
            failure=failure,
            result=result,
            schema_version=schema_version,
        )

        return recipe_image_availability_response
