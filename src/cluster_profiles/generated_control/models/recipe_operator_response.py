from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_operator_response_state import check_recipe_operator_response_state
from ..models.recipe_operator_response_state import RecipeOperatorResponseState
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.availability_operation_failure import AvailabilityOperationFailure
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
            with_model (bool):
            cancelled_builds (list[str] | Unset):
            cancelled_operations (list[str] | Unset):
            failure (AvailabilityOperationFailure | None | Unset):
            model_removals (list[str] | Unset):
            next_actions (list[str] | Unset):
            preserved (list[str] | Unset):
     """

    action: Literal['remove']
    operation_id: str
    progress: OperationProgress
    recipe_revision_id: str
    reclaimed_bytes: int
    request_key: str
    selector: str
    state: RecipeOperatorResponseState
    with_model: bool
    cancelled_builds: list[str] | Unset = UNSET
    cancelled_operations: list[str] | Unset = UNSET
    failure: AvailabilityOperationFailure | None | Unset = UNSET
    model_removals: list[str] | Unset = UNSET
    next_actions: list[str] | Unset = UNSET
    preserved: list[str] | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.availability_operation_failure import AvailabilityOperationFailure # noqa: PLC0415
        from ..models.operation_progress import OperationProgress # noqa: PLC0415
        action = self.action

        operation_id = self.operation_id

        progress = self.progress.to_dict()

        recipe_revision_id = self.recipe_revision_id

        reclaimed_bytes = self.reclaimed_bytes

        request_key = self.request_key

        selector = self.selector

        state: str = self.state

        with_model = self.with_model

        cancelled_builds: list[str] | Unset = UNSET
        if not isinstance(self.cancelled_builds, Unset):
            cancelled_builds = self.cancelled_builds



        cancelled_operations: list[str] | Unset = UNSET
        if not isinstance(self.cancelled_operations, Unset):
            cancelled_operations = self.cancelled_operations



        failure: dict[str, Any] | None | Unset
        if isinstance(self.failure, Unset):
            failure = UNSET
        elif isinstance(self.failure, AvailabilityOperationFailure):
            failure = self.failure.to_dict()
        else:
            failure = self.failure

        model_removals: list[str] | Unset = UNSET
        if not isinstance(self.model_removals, Unset):
            model_removals = self.model_removals



        next_actions: list[str] | Unset = UNSET
        if not isinstance(self.next_actions, Unset):
            next_actions = self.next_actions



        preserved: list[str] | Unset = UNSET
        if not isinstance(self.preserved, Unset):
            preserved = self.preserved




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
            "with_model": with_model,
        })
        if cancelled_builds is not UNSET:
            field_dict["cancelled_builds"] = cancelled_builds
        if cancelled_operations is not UNSET:
            field_dict["cancelled_operations"] = cancelled_operations
        if failure is not UNSET:
            field_dict["failure"] = failure
        if model_removals is not UNSET:
            field_dict["model_removals"] = model_removals
        if next_actions is not UNSET:
            field_dict["next_actions"] = next_actions
        if preserved is not UNSET:
            field_dict["preserved"] = preserved

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.availability_operation_failure import AvailabilityOperationFailure # noqa: PLC0415
        from ..models.operation_progress import OperationProgress # noqa: PLC0415
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




        with_model = d.pop("with_model")

        cancelled_builds = cast(list[str], d.pop("cancelled_builds", UNSET))


        cancelled_operations = cast(list[str], d.pop("cancelled_operations", UNSET))


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


        model_removals = cast(list[str], d.pop("model_removals", UNSET))


        next_actions = cast(list[str], d.pop("next_actions", UNSET))


        preserved = cast(list[str], d.pop("preserved", UNSET))


        recipe_operator_response = cls(
            action=action,
            operation_id=operation_id,
            progress=progress,
            recipe_revision_id=recipe_revision_id,
            reclaimed_bytes=reclaimed_bytes,
            request_key=request_key,
            selector=selector,
            state=state,
            with_model=with_model,
            cancelled_builds=cancelled_builds,
            cancelled_operations=cancelled_operations,
            failure=failure,
            model_removals=model_removals,
            next_actions=next_actions,
            preserved=preserved,
        )

        return recipe_operator_response
