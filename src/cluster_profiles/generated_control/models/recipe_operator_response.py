from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_operator_response_state import check_recipe_operator_response_state
from ..models.recipe_operator_response_state import RecipeOperatorResponseState
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast
from typing import Literal, Union, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.operation_progress import OperationProgress





T = TypeVar("T", bound="RecipeOperatorResponse")



@_attrs_define
class RecipeOperatorResponse:
    """
        Attributes:
            action (Literal['remove']):
            operation_id (str):
            progress (OperationProgress): Canonical durable progress payload shared by Controller and agents.
            recipe_revision_id (str):
            reclaimed_bytes (int):
            request_key (str):
            selector (str):
            state (RecipeOperatorResponseState):
            cancelled_builds (Union[Unset, list[str]]):
            cancelled_operations (Union[Unset, list[str]]):
            model_removals (Union[Unset, list[str]]):
            next_actions (Union[Unset, list[str]]):
            preserved (Union[Unset, list[str]]):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    action: Literal['remove']
    operation_id: str
    progress: 'OperationProgress'
    recipe_revision_id: str
    reclaimed_bytes: int
    request_key: str
    selector: str
    state: RecipeOperatorResponseState
    cancelled_builds: Union[Unset, list[str]] = UNSET
    cancelled_operations: Union[Unset, list[str]] = UNSET
    model_removals: Union[Unset, list[str]] = UNSET
    next_actions: Union[Unset, list[str]] = UNSET
    preserved: Union[Unset, list[str]] = UNSET
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.operation_progress import OperationProgress
        action = self.action

        operation_id = self.operation_id

        progress = self.progress.to_dict()

        recipe_revision_id = self.recipe_revision_id

        reclaimed_bytes = self.reclaimed_bytes

        request_key = self.request_key

        selector = self.selector

        state: str = self.state

        cancelled_builds: Union[Unset, list[str]] = UNSET
        if not isinstance(self.cancelled_builds, Unset):
            cancelled_builds = self.cancelled_builds



        cancelled_operations: Union[Unset, list[str]] = UNSET
        if not isinstance(self.cancelled_operations, Unset):
            cancelled_operations = self.cancelled_operations



        model_removals: Union[Unset, list[str]] = UNSET
        if not isinstance(self.model_removals, Unset):
            model_removals = self.model_removals



        next_actions: Union[Unset, list[str]] = UNSET
        if not isinstance(self.next_actions, Unset):
            next_actions = self.next_actions



        preserved: Union[Unset, list[str]] = UNSET
        if not isinstance(self.preserved, Unset):
            preserved = self.preserved



        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "action": action,
            "operation_id": operation_id,
            "progress": progress,
            "recipe_revision_id": recipe_revision_id,
            "reclaimed_bytes": reclaimed_bytes,
            "request_key": request_key,
            "selector": selector,
            "state": state,
        })
        if cancelled_builds is not UNSET:
            field_dict["cancelled_builds"] = cancelled_builds
        if cancelled_operations is not UNSET:
            field_dict["cancelled_operations"] = cancelled_operations
        if model_removals is not UNSET:
            field_dict["model_removals"] = model_removals
        if next_actions is not UNSET:
            field_dict["next_actions"] = next_actions
        if preserved is not UNSET:
            field_dict["preserved"] = preserved
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.operation_progress import OperationProgress
        d = dict(src_dict)
        action = cast(Literal['remove'] , d.pop("action"))
        if action != 'remove':
            raise ValueError(f"action must match const 'remove', got '{action}'")

        operation_id = d.pop("operation_id")

        progress = OperationProgress.from_dict(d.pop("progress"))




        recipe_revision_id = d.pop("recipe_revision_id")

        reclaimed_bytes = d.pop("reclaimed_bytes")

        request_key = d.pop("request_key")

        selector = d.pop("selector")

        state = check_recipe_operator_response_state(d.pop("state"))




        cancelled_builds = cast(list[str], d.pop("cancelled_builds", UNSET))


        cancelled_operations = cast(list[str], d.pop("cancelled_operations", UNSET))


        model_removals = cast(list[str], d.pop("model_removals", UNSET))


        next_actions = cast(list[str], d.pop("next_actions", UNSET))


        preserved = cast(list[str], d.pop("preserved", UNSET))


        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        recipe_operator_response = cls(
            action=action,
            operation_id=operation_id,
            progress=progress,
            recipe_revision_id=recipe_revision_id,
            reclaimed_bytes=reclaimed_bytes,
            request_key=request_key,
            selector=selector,
            state=state,
            cancelled_builds=cancelled_builds,
            cancelled_operations=cancelled_operations,
            model_removals=model_removals,
            next_actions=next_actions,
            preserved=preserved,
            schema_version=schema_version,
        )

        return recipe_operator_response
