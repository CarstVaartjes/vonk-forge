from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Union
import datetime

if TYPE_CHECKING:
  from ..models.recipe_readiness_check import RecipeReadinessCheck
  from ..models.spark_group import SparkGroup
  from ..models.spark_fit import SparkFit





T = TypeVar("T", bound="RecipeReadiness")



@_attrs_define
class RecipeReadiness:
    """
        Attributes:
            cache (RecipeReadinessCheck):
            fleet_fit (RecipeReadinessCheck):
            observed_at (datetime.datetime):
            readiness (RecipeReadinessCheck):
            fit (Union['SparkFit', None, Unset]):
            group (Union['SparkGroup', None, Unset]):
     """

    cache: 'RecipeReadinessCheck'
    fleet_fit: 'RecipeReadinessCheck'
    observed_at: datetime.datetime
    readiness: 'RecipeReadinessCheck'
    fit: Union['SparkFit', None, Unset] = UNSET
    group: Union['SparkGroup', None, Unset] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_readiness_check import RecipeReadinessCheck
        from ..models.spark_group import SparkGroup
        from ..models.spark_fit import SparkFit
        cache = self.cache.to_dict()

        fleet_fit = self.fleet_fit.to_dict()

        observed_at = self.observed_at.isoformat()

        readiness = self.readiness.to_dict()

        fit: Union[None, Unset, dict[str, Any]]
        if isinstance(self.fit, Unset):
            fit = UNSET
        elif isinstance(self.fit, SparkFit):
            fit = self.fit.to_dict()
        else:
            fit = self.fit

        group: Union[None, Unset, dict[str, Any]]
        if isinstance(self.group, Unset):
            group = UNSET
        elif isinstance(self.group, SparkGroup):
            group = self.group.to_dict()
        else:
            group = self.group


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "cache": cache,
            "fleet_fit": fleet_fit,
            "observed_at": observed_at,
            "readiness": readiness,
        })
        if fit is not UNSET:
            field_dict["fit"] = fit
        if group is not UNSET:
            field_dict["group"] = group

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_readiness_check import RecipeReadinessCheck
        from ..models.spark_group import SparkGroup
        from ..models.spark_fit import SparkFit
        d = dict(src_dict)
        cache = RecipeReadinessCheck.from_dict(d.pop("cache"))




        fleet_fit = RecipeReadinessCheck.from_dict(d.pop("fleet_fit"))




        observed_at = isoparse(d.pop("observed_at"))




        readiness = RecipeReadinessCheck.from_dict(d.pop("readiness"))




        def _parse_fit(data: object) -> Union['SparkFit', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                fit_type_0 = SparkFit.from_dict(data)



                return fit_type_0
            except: # noqa: E722
                pass
            return cast(Union['SparkFit', None, Unset], data)

        fit = _parse_fit(d.pop("fit", UNSET))


        def _parse_group(data: object) -> Union['SparkGroup', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                group_type_0 = SparkGroup.from_dict(data)



                return group_type_0
            except: # noqa: E722
                pass
            return cast(Union['SparkGroup', None, Unset], data)

        group = _parse_group(d.pop("group", UNSET))


        recipe_readiness = cls(
            cache=cache,
            fleet_fit=fleet_fit,
            observed_at=observed_at,
            readiness=readiness,
            fit=fit,
            group=group,
        )

        return recipe_readiness
