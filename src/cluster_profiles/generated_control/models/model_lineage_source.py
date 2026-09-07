from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import Literal, Union, cast






T = TypeVar("T", bound="ModelLineageSource")



@_attrs_define
class ModelLineageSource:
    """
        Attributes:
            publisher (str):
            slug (str):
            kind (Union[Literal['model'], Unset]):  Default: 'model'.
     """

    publisher: str
    slug: str
    kind: Union[Literal['model'], Unset] = 'model'





    def to_dict(self) -> dict[str, Any]:
        publisher = self.publisher

        slug = self.slug

        kind = self.kind


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "publisher": publisher,
            "slug": slug,
        })
        if kind is not UNSET:
            field_dict["kind"] = kind

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        publisher = d.pop("publisher")

        slug = d.pop("slug")

        kind = cast(Union[Literal['model'], Unset] , d.pop("kind", UNSET))
        if kind != 'model' and not isinstance(kind, Unset):
            raise ValueError(f"kind must match const 'model', got '{kind}'")

        model_lineage_source = cls(
            publisher=publisher,
            slug=slug,
            kind=kind,
        )

        return model_lineage_source
