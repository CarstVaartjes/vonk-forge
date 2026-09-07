from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.recipe_integer_setting import RecipeIntegerSetting
  from ..models.recipe_job_settings_knobs import RecipeJobSettingsKnobs





T = TypeVar("T", bound="RecipeJobSettings")



@_attrs_define
class RecipeJobSettings:
    """
        Attributes:
            kind (Literal['job']):
            concurrency (Union['RecipeIntegerSetting', None, Unset]):
            knobs (Union[Unset, RecipeJobSettingsKnobs]):
     """

    kind: Literal['job']
    concurrency: Union['RecipeIntegerSetting', None, Unset] = UNSET
    knobs: Union[Unset, 'RecipeJobSettingsKnobs'] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_integer_setting import RecipeIntegerSetting
        from ..models.recipe_job_settings_knobs import RecipeJobSettingsKnobs
        kind = self.kind

        concurrency: Union[None, Unset, dict[str, Any]]
        if isinstance(self.concurrency, Unset):
            concurrency = UNSET
        elif isinstance(self.concurrency, RecipeIntegerSetting):
            concurrency = self.concurrency.to_dict()
        else:
            concurrency = self.concurrency

        knobs: Union[Unset, dict[str, Any]] = UNSET
        if not isinstance(self.knobs, Unset):
            knobs = self.knobs.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "kind": kind,
        })
        if concurrency is not UNSET:
            field_dict["concurrency"] = concurrency
        if knobs is not UNSET:
            field_dict["knobs"] = knobs

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_integer_setting import RecipeIntegerSetting
        from ..models.recipe_job_settings_knobs import RecipeJobSettingsKnobs
        d = dict(src_dict)
        kind = cast(Literal['job'] , d.pop("kind"))
        if kind != 'job':
            raise ValueError(f"kind must match const 'job', got '{kind}'")

        def _parse_concurrency(data: object) -> Union['RecipeIntegerSetting', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                concurrency_type_0 = RecipeIntegerSetting.from_dict(data)



                return concurrency_type_0
            except: # noqa: E722
                pass
            return cast(Union['RecipeIntegerSetting', None, Unset], data)

        concurrency = _parse_concurrency(d.pop("concurrency", UNSET))


        _knobs = d.pop("knobs", UNSET)
        knobs: Union[Unset, RecipeJobSettingsKnobs]
        if isinstance(_knobs,  Unset):
            knobs = UNSET
        else:
            knobs = RecipeJobSettingsKnobs.from_dict(_knobs)




        recipe_job_settings = cls(
            kind=kind,
            concurrency=concurrency,
            knobs=knobs,
        )

        return recipe_job_settings
