from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, Union, cast

if TYPE_CHECKING:
  from ..models.build_preview_input import BuildPreviewInput





T = TypeVar("T", bound="BuildPreviewTarget")



@_attrs_define
class BuildPreviewTarget:
    """
        Attributes:
            input_ (BuildPreviewInput):
            kind (Union[Literal['build'], Unset]):  Default: 'build'.
     """

    input_: 'BuildPreviewInput'
    kind: Union[Literal['build'], Unset] = 'build'





    def to_dict(self) -> dict[str, Any]:
        from ..models.build_preview_input import BuildPreviewInput
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
        from ..models.build_preview_input import BuildPreviewInput
        d = dict(src_dict)
        input_ = BuildPreviewInput.from_dict(d.pop("input"))




        kind = cast(Union[Literal['build'], Unset] , d.pop("kind", UNSET))
        if kind != 'build' and not isinstance(kind, Unset):
            raise ValueError(f"kind must match const 'build', got '{kind}'")

        build_preview_target = cls(
            input_=input_,
            kind=kind,
        )

        return build_preview_target
