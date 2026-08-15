from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, Union, cast

if TYPE_CHECKING:
  from ..models.install_preview_input import InstallPreviewInput





T = TypeVar("T", bound="InstallPreviewTarget")



@_attrs_define
class InstallPreviewTarget:
    """
        Attributes:
            input_ (InstallPreviewInput):
            kind (Union[Literal['install'], Unset]):  Default: 'install'.
     """

    input_: 'InstallPreviewInput'
    kind: Union[Literal['install'], Unset] = 'install'





    def to_dict(self) -> dict[str, Any]:
        from ..models.install_preview_input import InstallPreviewInput
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
        from ..models.install_preview_input import InstallPreviewInput
        d = dict(src_dict)
        input_ = InstallPreviewInput.from_dict(d.pop("input"))




        kind = cast(Union[Literal['install'], Unset] , d.pop("kind", UNSET))
        if kind != 'install' and not isinstance(kind, Unset):
            raise ValueError(f"kind must match const 'install', got '{kind}'")

        install_preview_target = cls(
            input_=input_,
            kind=kind,
        )

        return install_preview_target
