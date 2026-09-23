from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import Literal, Union, cast






T = TypeVar("T", bound="RecipeRetryIntent")



@_attrs_define
class RecipeRetryIntent:
    """
        Attributes:
            operation_id (str):
            kind (Union[Literal['retry'], Unset]):  Default: 'retry'.
     """

    operation_id: str
    kind: Union[Literal['retry'], Unset] = 'retry'





    def to_dict(self) -> dict[str, Any]:
        operation_id = self.operation_id

        kind = self.kind


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "operation_id": operation_id,
        })
        if kind is not UNSET:
            field_dict["kind"] = kind

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        operation_id = d.pop("operation_id")

        kind = cast(Union[Literal['retry'], Unset] , d.pop("kind", UNSET))
        if kind != 'retry' and not isinstance(kind, Unset):
            raise ValueError(f"kind must match const 'retry', got '{kind}'")

        recipe_retry_intent = cls(
            operation_id=operation_id,
            kind=kind,
        )

        return recipe_retry_intent
