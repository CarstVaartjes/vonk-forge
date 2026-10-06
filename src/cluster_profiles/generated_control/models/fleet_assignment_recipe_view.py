from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="FleetAssignmentRecipeView")



@_attrs_define
class FleetAssignmentRecipeView:
    """ The recipe revision an assignment runs, as the operator reads it.

        Attributes:
            selector (str):
            state (str):
            name (None | str | Unset):
            revision_id (None | str | Unset):
     """

    selector: str
    state: str
    name: None | str | Unset = UNSET
    revision_id: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        selector = self.selector

        state = self.state

        name: None | str | Unset
        if isinstance(self.name, Unset):
            name = UNSET
        else:
            name = self.name

        revision_id: None | str | Unset
        if isinstance(self.revision_id, Unset):
            revision_id = UNSET
        else:
            revision_id = self.revision_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "selector": selector,
            "state": state,
        })
        if name is not UNSET:
            field_dict["name"] = name
        if revision_id is not UNSET:
            field_dict["revision_id"] = revision_id

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        selector = d.pop("selector")

        state = d.pop("state")

        def _parse_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        name = _parse_name(d.pop("name", UNSET))


        def _parse_revision_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        revision_id = _parse_revision_id(d.pop("revision_id", UNSET))


        fleet_assignment_recipe_view = cls(
            selector=selector,
            state=state,
            name=name,
            revision_id=revision_id,
        )

        return fleet_assignment_recipe_view
