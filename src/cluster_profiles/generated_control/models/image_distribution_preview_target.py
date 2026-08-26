from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, Union, cast

if TYPE_CHECKING:
  from ..models.image_distribution_preview_input import ImageDistributionPreviewInput





T = TypeVar("T", bound="ImageDistributionPreviewTarget")



@_attrs_define
class ImageDistributionPreviewTarget:
    """
        Attributes:
            input_ (ImageDistributionPreviewInput):
            kind (Union[Literal['image_distribution'], Unset]):  Default: 'image_distribution'.
     """

    input_: 'ImageDistributionPreviewInput'
    kind: Union[Literal['image_distribution'], Unset] = 'image_distribution'





    def to_dict(self) -> dict[str, Any]:
        from ..models.image_distribution_preview_input import ImageDistributionPreviewInput
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
        from ..models.image_distribution_preview_input import ImageDistributionPreviewInput
        d = dict(src_dict)
        input_ = ImageDistributionPreviewInput.from_dict(d.pop("input"))




        kind = cast(Union[Literal['image_distribution'], Unset] , d.pop("kind", UNSET))
        if kind != 'image_distribution' and not isinstance(kind, Unset):
            raise ValueError(f"kind must match const 'image_distribution', got '{kind}'")

        image_distribution_preview_target = cls(
            input_=input_,
            kind=kind,
        )

        return image_distribution_preview_target
