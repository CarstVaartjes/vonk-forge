from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.visual_territorial_restrictions import VisualTerritorialRestrictions





T = TypeVar("T", bound="VisualModelLicense")



@_attrs_define
class VisualModelLicense:
    """
        Attributes:
            territorial_restrictions (Union['VisualTerritorialRestrictions', None, Unset]):
     """

    territorial_restrictions: Union['VisualTerritorialRestrictions', None, Unset] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.visual_territorial_restrictions import VisualTerritorialRestrictions
        territorial_restrictions: Union[None, Unset, dict[str, Any]]
        if isinstance(self.territorial_restrictions, Unset):
            territorial_restrictions = UNSET
        elif isinstance(self.territorial_restrictions, VisualTerritorialRestrictions):
            territorial_restrictions = self.territorial_restrictions.to_dict()
        else:
            territorial_restrictions = self.territorial_restrictions


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if territorial_restrictions is not UNSET:
            field_dict["territorial_restrictions"] = territorial_restrictions

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.visual_territorial_restrictions import VisualTerritorialRestrictions
        d = dict(src_dict)
        def _parse_territorial_restrictions(data: object) -> Union['VisualTerritorialRestrictions', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                territorial_restrictions_type_0 = VisualTerritorialRestrictions.from_dict(data)



                return territorial_restrictions_type_0
            except: # noqa: E722
                pass
            return cast(Union['VisualTerritorialRestrictions', None, Unset], data)

        territorial_restrictions = _parse_territorial_restrictions(d.pop("territorial_restrictions", UNSET))


        visual_model_license = cls(
            territorial_restrictions=territorial_restrictions,
        )

        return visual_model_license
