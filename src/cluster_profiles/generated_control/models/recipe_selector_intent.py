from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import Literal, Union, cast
from typing import Union






T = TypeVar("T", bound="RecipeSelectorIntent")



@_attrs_define
class RecipeSelectorIntent:
    """
        Attributes:
            selector (str):
            force (Union[Unset, bool]):  Default: False.
            kind (Union[Literal['selector'], Unset]):  Default: 'selector'.
     """

    selector: str
    force: Union[Unset, bool] = False
    kind: Union[Literal['selector'], Unset] = 'selector'





    def to_dict(self) -> dict[str, Any]:
        selector = self.selector

        force = self.force

        kind = self.kind


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "selector": selector,
        })
        if force is not UNSET:
            field_dict["force"] = force
        if kind is not UNSET:
            field_dict["kind"] = kind

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        selector = d.pop("selector")

        force = d.pop("force", UNSET)

        kind = cast(Union[Literal['selector'], Unset] , d.pop("kind", UNSET))
        if kind != 'selector' and not isinstance(kind, Unset):
            raise ValueError(f"kind must match const 'selector', got '{kind}'")

        recipe_selector_intent = cls(
            selector=selector,
            force=force,
            kind=kind,
        )

        return recipe_selector_intent
