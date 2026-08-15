from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, Union, cast

if TYPE_CHECKING:
  from ..models.run_preview_input import RunPreviewInput





T = TypeVar("T", bound="RunPreviewTarget")



@_attrs_define
class RunPreviewTarget:
    """
        Attributes:
            input_ (RunPreviewInput):
            kind (Union[Literal['run'], Unset]):  Default: 'run'.
     """

    input_: 'RunPreviewInput'
    kind: Union[Literal['run'], Unset] = 'run'





    def to_dict(self) -> dict[str, Any]:
        from ..models.run_preview_input import RunPreviewInput
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
        from ..models.run_preview_input import RunPreviewInput
        d = dict(src_dict)
        input_ = RunPreviewInput.from_dict(d.pop("input"))




        kind = cast(Union[Literal['run'], Unset] , d.pop("kind", UNSET))
        if kind != 'run'and not isinstance(kind, Unset):
            raise ValueError(f"kind must match const 'run', got '{kind}'")

        run_preview_target = cls(
            input_=input_,
            kind=kind,
        )

        return run_preview_target
