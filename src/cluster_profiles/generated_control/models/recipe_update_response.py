from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_update_response_state import check_recipe_update_response_state
from ..models.recipe_update_response_state import RecipeUpdateResponseState
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast
import datetime

if TYPE_CHECKING:
  from ..models.operation_progress import OperationProgress
  from ..models.recipe_operation_cancellation_result import RecipeOperationCancellationResult
  from ..models.recipe_update_child import RecipeUpdateChild
  from ..models.recipe_update_scope import RecipeUpdateScope





T = TypeVar("T", bound="RecipeUpdateResponse")



@_attrs_define
class RecipeUpdateResponse:
    """
        Attributes:
            attempt (int):
            children (list[RecipeUpdateChild]):
            created_at (datetime.datetime):
            id (str):
            progress (OperationProgress): Canonical durable progress payload shared by Controller and agents.
            request (RecipeUpdateScope):
            request_id (str):
            state (RecipeUpdateResponseState):
            updated_at (datetime.datetime):
            action (Literal['update'] | Unset):  Default: 'update'.
            cancellation (None | RecipeOperationCancellationResult | Unset):
            kind (Literal['recipe.cache.update.v2'] | Unset):  Default: 'recipe.cache.update.v2'.
            next_attempt_at (datetime.datetime | None | Unset):
            resume_condition (None | str | Unset):
            wait_owner (Literal['recipe-image-availability'] | None | Unset):
            waiting_on (None | str | Unset):
     """

    attempt: int
    children: list[RecipeUpdateChild]
    created_at: datetime.datetime
    id: str
    progress: OperationProgress
    request: RecipeUpdateScope
    request_id: str
    state: RecipeUpdateResponseState
    updated_at: datetime.datetime
    action: Literal['update'] | Unset = 'update'
    cancellation: None | RecipeOperationCancellationResult | Unset = UNSET
    kind: Literal['recipe.cache.update.v2'] | Unset = 'recipe.cache.update.v2'
    next_attempt_at: datetime.datetime | None | Unset = UNSET
    resume_condition: None | str | Unset = UNSET
    wait_owner: Literal['recipe-image-availability'] | None | Unset = UNSET
    waiting_on: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.operation_progress import OperationProgress # noqa: PLC0415
        from ..models.recipe_operation_cancellation_result import RecipeOperationCancellationResult # noqa: PLC0415
        from ..models.recipe_update_child import RecipeUpdateChild # noqa: PLC0415
        from ..models.recipe_update_scope import RecipeUpdateScope # noqa: PLC0415
        attempt = self.attempt

        children = []
        for children_item_data in self.children:
            children_item = children_item_data.to_dict()
            children.append(children_item)



        created_at = self.created_at.isoformat()

        id = self.id

        progress = self.progress.to_dict()

        request = self.request.to_dict()

        request_id = self.request_id

        state: str = self.state

        updated_at = self.updated_at.isoformat()

        action = self.action

        cancellation: dict[str, Any] | None | Unset
        if isinstance(self.cancellation, Unset):
            cancellation = UNSET
        elif isinstance(self.cancellation, RecipeOperationCancellationResult):
            cancellation = self.cancellation.to_dict()
        else:
            cancellation = self.cancellation

        kind = self.kind

        next_attempt_at: None | str | Unset
        if isinstance(self.next_attempt_at, Unset):
            next_attempt_at = UNSET
        elif isinstance(self.next_attempt_at, datetime.datetime):
            next_attempt_at = self.next_attempt_at.isoformat()
        else:
            next_attempt_at = self.next_attempt_at

        resume_condition: None | str | Unset
        if isinstance(self.resume_condition, Unset):
            resume_condition = UNSET
        else:
            resume_condition = self.resume_condition

        wait_owner: Literal['recipe-image-availability'] | None | Unset
        if isinstance(self.wait_owner, Unset):
            wait_owner = UNSET
        else:
            wait_owner = self.wait_owner

        waiting_on: None | str | Unset
        if isinstance(self.waiting_on, Unset):
            waiting_on = UNSET
        else:
            waiting_on = self.waiting_on


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "attempt": attempt,
            "children": children,
            "created_at": created_at,
            "id": id,
            "progress": progress,
            "request": request,
            "request_id": request_id,
            "state": state,
            "updated_at": updated_at,
        })
        if action is not UNSET:
            field_dict["action"] = action
        if cancellation is not UNSET:
            field_dict["cancellation"] = cancellation
        if kind is not UNSET:
            field_dict["kind"] = kind
        if next_attempt_at is not UNSET:
            field_dict["next_attempt_at"] = next_attempt_at
        if resume_condition is not UNSET:
            field_dict["resume_condition"] = resume_condition
        if wait_owner is not UNSET:
            field_dict["wait_owner"] = wait_owner
        if waiting_on is not UNSET:
            field_dict["waiting_on"] = waiting_on

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.operation_progress import OperationProgress # noqa: PLC0415
        from ..models.recipe_operation_cancellation_result import RecipeOperationCancellationResult # noqa: PLC0415
        from ..models.recipe_update_child import RecipeUpdateChild # noqa: PLC0415
        from ..models.recipe_update_scope import RecipeUpdateScope # noqa: PLC0415
        d = dict(src_dict)
        attempt = d.pop("attempt")

        children = []
        _children = d.pop("children")
        for children_item_data in (_children):
            children_item = RecipeUpdateChild.from_dict(children_item_data)



            children.append(children_item)


        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))




        id = d.pop("id")

        progress = OperationProgress.from_dict(d.pop("progress"))




        request = RecipeUpdateScope.from_dict(d.pop("request"))




        request_id = d.pop("request_id")

        state = check_recipe_update_response_state(d.pop("state"))




        updated_at = datetime.datetime.fromisoformat(d.pop("updated_at"))




        action = cast(Literal['update'] | Unset , d.pop("action", UNSET))
        if action != 'update' and not isinstance(action, Unset):
            raise ValueError(f"action must match const 'update', got '{action}'")

        def _parse_cancellation(data: object) -> None | RecipeOperationCancellationResult | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                cancellation_type_0 = RecipeOperationCancellationResult.from_dict(data)



                return cancellation_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RecipeOperationCancellationResult | Unset, data)

        cancellation = _parse_cancellation(d.pop("cancellation", UNSET))


        kind = cast(Literal['recipe.cache.update.v2'] | Unset , d.pop("kind", UNSET))
        if kind != 'recipe.cache.update.v2' and not isinstance(kind, Unset):
            raise ValueError(f"kind must match const 'recipe.cache.update.v2', got '{kind}'")

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


        def _parse_resume_condition(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        resume_condition = _parse_resume_condition(d.pop("resume_condition", UNSET))


        def _parse_wait_owner(data: object) -> Literal['recipe-image-availability'] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            wait_owner_type_0 = cast(Literal['recipe-image-availability'] , data)
            if wait_owner_type_0 != 'recipe-image-availability':
                raise ValueError(f"wait_owner_type_0 must match const 'recipe-image-availability', got '{wait_owner_type_0}'")
            return wait_owner_type_0
            return cast(Literal['recipe-image-availability'] | None | Unset, data)

        wait_owner = _parse_wait_owner(d.pop("wait_owner", UNSET))


        def _parse_waiting_on(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        waiting_on = _parse_waiting_on(d.pop("waiting_on", UNSET))


        recipe_update_response = cls(
            attempt=attempt,
            children=children,
            created_at=created_at,
            id=id,
            progress=progress,
            request=request,
            request_id=request_id,
            state=state,
            updated_at=updated_at,
            action=action,
            cancellation=cancellation,
            kind=kind,
            next_attempt_at=next_attempt_at,
            resume_condition=resume_condition,
            wait_owner=wait_owner,
            waiting_on=waiting_on,
        )


        recipe_update_response.additional_properties = d
        return recipe_update_response

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
