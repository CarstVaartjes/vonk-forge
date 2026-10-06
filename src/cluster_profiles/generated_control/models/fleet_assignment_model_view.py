from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="FleetAssignmentModelView")



@_attrs_define
class FleetAssignmentModelView:
    """ The model an assignment runs, as the operator reads it.

        Attributes:
            state (str):
            content_sha256 (None | str | Unset):
            name (None | str | Unset):
            selector (None | str | Unset):
            variant (None | str | Unset):
     """

    state: str
    content_sha256: None | str | Unset = UNSET
    name: None | str | Unset = UNSET
    selector: None | str | Unset = UNSET
    variant: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        state = self.state

        content_sha256: None | str | Unset
        if isinstance(self.content_sha256, Unset):
            content_sha256 = UNSET
        else:
            content_sha256 = self.content_sha256

        name: None | str | Unset
        if isinstance(self.name, Unset):
            name = UNSET
        else:
            name = self.name

        selector: None | str | Unset
        if isinstance(self.selector, Unset):
            selector = UNSET
        else:
            selector = self.selector

        variant: None | str | Unset
        if isinstance(self.variant, Unset):
            variant = UNSET
        else:
            variant = self.variant


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "state": state,
        })
        if content_sha256 is not UNSET:
            field_dict["content_sha256"] = content_sha256
        if name is not UNSET:
            field_dict["name"] = name
        if selector is not UNSET:
            field_dict["selector"] = selector
        if variant is not UNSET:
            field_dict["variant"] = variant

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        state = d.pop("state")

        def _parse_content_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        content_sha256 = _parse_content_sha256(d.pop("content_sha256", UNSET))


        def _parse_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        name = _parse_name(d.pop("name", UNSET))


        def _parse_selector(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        selector = _parse_selector(d.pop("selector", UNSET))


        def _parse_variant(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        variant = _parse_variant(d.pop("variant", UNSET))


        fleet_assignment_model_view = cls(
            state=state,
            content_sha256=content_sha256,
            name=name,
            selector=selector,
            variant=variant,
        )

        return fleet_assignment_model_view
