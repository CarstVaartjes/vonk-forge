from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.recipe_removal_projection_issue import RecipeRemovalProjectionIssue





T = TypeVar("T", bound="RecipeRemovalUnavailableView")



@_attrs_define
class RecipeRemovalUnavailableView:
    """ Known Job identity with no claim about unreadable removal effects.

        Attributes:
            observed_at (str):
            operation_id (str):
            projection_issue (RecipeRemovalProjectionIssue):
            recipe_revision_id (None | str):
            request_key (None | str):
            action (Literal['remove'] | Unset):  Default: 'remove'.
            failure (None | Unset):
            kind (Literal['recipe.cache.remove.v2'] | Unset):  Default: 'recipe.cache.remove.v2'.
            progress (None | Unset):
            state (Literal['unknown'] | Unset):  Default: 'unknown'.
     """

    observed_at: str
    operation_id: str
    projection_issue: RecipeRemovalProjectionIssue
    recipe_revision_id: None | str
    request_key: None | str
    action: Literal['remove'] | Unset = 'remove'
    failure: None | Unset = UNSET
    kind: Literal['recipe.cache.remove.v2'] | Unset = 'recipe.cache.remove.v2'
    progress: None | Unset = UNSET
    state: Literal['unknown'] | Unset = 'unknown'
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_removal_projection_issue import RecipeRemovalProjectionIssue # noqa: PLC0415
        observed_at = self.observed_at

        operation_id = self.operation_id

        projection_issue = self.projection_issue.to_dict()

        recipe_revision_id: None | str
        recipe_revision_id = self.recipe_revision_id

        request_key: None | str
        request_key = self.request_key

        action = self.action

        failure = self.failure

        kind = self.kind

        progress = self.progress

        state = self.state


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "observed_at": observed_at,
            "operation_id": operation_id,
            "projection_issue": projection_issue,
            "recipe_revision_id": recipe_revision_id,
            "request_key": request_key,
        })
        if action is not UNSET:
            field_dict["action"] = action
        if failure is not UNSET:
            field_dict["failure"] = failure
        if kind is not UNSET:
            field_dict["kind"] = kind
        if progress is not UNSET:
            field_dict["progress"] = progress
        if state is not UNSET:
            field_dict["state"] = state

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_removal_projection_issue import RecipeRemovalProjectionIssue # noqa: PLC0415
        d = dict(src_dict)
        observed_at = d.pop("observed_at")

        operation_id = d.pop("operation_id")

        projection_issue = RecipeRemovalProjectionIssue.from_dict(d.pop("projection_issue"))




        def _parse_recipe_revision_id(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        recipe_revision_id = _parse_recipe_revision_id(d.pop("recipe_revision_id"))


        def _parse_request_key(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        request_key = _parse_request_key(d.pop("request_key"))


        action = cast(Literal['remove'] | Unset , d.pop("action", UNSET))
        if action != 'remove' and not isinstance(action, Unset):
            raise ValueError(f"action must match const 'remove', got '{action}'")

        failure = d.pop("failure", UNSET)

        kind = cast(Literal['recipe.cache.remove.v2'] | Unset , d.pop("kind", UNSET))
        if kind != 'recipe.cache.remove.v2' and not isinstance(kind, Unset):
            raise ValueError(f"kind must match const 'recipe.cache.remove.v2', got '{kind}'")

        progress = d.pop("progress", UNSET)

        state = cast(Literal['unknown'] | Unset , d.pop("state", UNSET))
        if state != 'unknown' and not isinstance(state, Unset):
            raise ValueError(f"state must match const 'unknown', got '{state}'")

        recipe_removal_unavailable_view = cls(
            observed_at=observed_at,
            operation_id=operation_id,
            projection_issue=projection_issue,
            recipe_revision_id=recipe_revision_id,
            request_key=request_key,
            action=action,
            failure=failure,
            kind=kind,
            progress=progress,
            state=state,
        )


        recipe_removal_unavailable_view.additional_properties = d
        return recipe_removal_unavailable_view

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
