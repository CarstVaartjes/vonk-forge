from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.visual_provenance_source_kind import check_visual_provenance_source_kind
from ..models.visual_provenance_source_kind import VisualProvenanceSourceKind
from typing import cast
from typing import cast, Union






T = TypeVar("T", bound="VisualProvenance")



@_attrs_define
class VisualProvenance:
    """
        Attributes:
            attribution (list[str]):
            source_kind (VisualProvenanceSourceKind):
            source_reference (Union[None, str]):
     """

    attribution: list[str]
    source_kind: VisualProvenanceSourceKind
    source_reference: Union[None, str]





    def to_dict(self) -> dict[str, Any]:
        attribution = self.attribution



        source_kind: str = self.source_kind

        source_reference: Union[None, str]
        source_reference = self.source_reference


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "attribution": attribution,
            "source_kind": source_kind,
            "source_reference": source_reference,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        attribution = cast(list[str], d.pop("attribution"))


        source_kind = check_visual_provenance_source_kind(d.pop("source_kind"))




        def _parse_source_reference(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        source_reference = _parse_source_reference(d.pop("source_reference"))


        visual_provenance = cls(
            attribution=attribution,
            source_kind=source_kind,
            source_reference=source_reference,
        )

        return visual_provenance
