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
  from ..models.model_territorial_restrictions import ModelTerritorialRestrictions





T = TypeVar("T", bound="ModelLicense")



@_attrs_define
class ModelLicense:
    """
        Attributes:
            attribution (list[str]):
            operator_acceptance_required (bool):
            spdx (str):
            url (str):
            territorial_restrictions (Union['ModelTerritorialRestrictions', None, Unset]):
     """

    attribution: list[str]
    operator_acceptance_required: bool
    spdx: str
    url: str
    territorial_restrictions: Union['ModelTerritorialRestrictions', None, Unset] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.model_territorial_restrictions import ModelTerritorialRestrictions
        attribution = self.attribution



        operator_acceptance_required = self.operator_acceptance_required

        spdx = self.spdx

        url = self.url

        territorial_restrictions: Union[None, Unset, dict[str, Any]]
        if isinstance(self.territorial_restrictions, Unset):
            territorial_restrictions = UNSET
        elif isinstance(self.territorial_restrictions, ModelTerritorialRestrictions):
            territorial_restrictions = self.territorial_restrictions.to_dict()
        else:
            territorial_restrictions = self.territorial_restrictions


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "attribution": attribution,
            "operator_acceptance_required": operator_acceptance_required,
            "spdx": spdx,
            "url": url,
        })
        if territorial_restrictions is not UNSET:
            field_dict["territorial_restrictions"] = territorial_restrictions

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.model_territorial_restrictions import ModelTerritorialRestrictions
        d = dict(src_dict)
        attribution = cast(list[str], d.pop("attribution"))


        operator_acceptance_required = d.pop("operator_acceptance_required")

        spdx = d.pop("spdx")

        url = d.pop("url")

        def _parse_territorial_restrictions(data: object) -> Union['ModelTerritorialRestrictions', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                territorial_restrictions_type_0 = ModelTerritorialRestrictions.from_dict(data)



                return territorial_restrictions_type_0
            except: # noqa: E722
                pass
            return cast(Union['ModelTerritorialRestrictions', None, Unset], data)

        territorial_restrictions = _parse_territorial_restrictions(d.pop("territorial_restrictions", UNSET))


        model_license = cls(
            attribution=attribution,
            operator_acceptance_required=operator_acceptance_required,
            spdx=spdx,
            url=url,
            territorial_restrictions=territorial_restrictions,
        )

        return model_license
