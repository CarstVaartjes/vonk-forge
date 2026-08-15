from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, Union, cast

if TYPE_CHECKING:
  from ..models.mapping_preview_input import MappingPreviewInput





T = TypeVar("T", bound="MappingPreviewTarget")



@_attrs_define
class MappingPreviewTarget:
    """
        Attributes:
            input_ (MappingPreviewInput):
            kind (Union[Literal['mapping'], Unset]):  Default: 'mapping'.
     """

    input_: 'MappingPreviewInput'
    kind: Union[Literal['mapping'], Unset] = 'mapping'





    def to_dict(self) -> dict[str, Any]:
        from ..models.mapping_preview_input import MappingPreviewInput
        input_ = self.input_.to_dict()

        kind = self.kind


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "input": input_,
        })
        if kind is not UNSET:
            field_dict["kind"] = kind

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.mapping_preview_input import MappingPreviewInput
        d = dict(src_dict)
        input_ = MappingPreviewInput.from_dict(d.pop("input"))




        kind = cast(Union[Literal['mapping'], Unset] , d.pop("kind", UNSET))
        if kind != 'mapping' and not isinstance(kind, Unset):
            raise ValueError(f"kind must match const 'mapping', got '{kind}'")

        mapping_preview_target = cls(
            input_=input_,
            kind=kind,
        )

        return mapping_preview_target
