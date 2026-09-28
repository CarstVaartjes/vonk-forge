from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
import datetime

if TYPE_CHECKING:
  from ..models.recipe_readiness_check import RecipeReadinessCheck
  from ..models.spark_fit import SparkFit
  from ..models.spark_group import SparkGroup





T = TypeVar("T", bound="RecipeReadiness")



@_attrs_define
class RecipeReadiness:
    """
        Attributes:
            cache (RecipeReadinessCheck):
            fleet_fit (RecipeReadinessCheck):
            observed_at (datetime.datetime):
            readiness (RecipeReadinessCheck):
            fit (None | SparkFit | Unset):
            group (None | SparkGroup | Unset):
     """

    cache: RecipeReadinessCheck
    fleet_fit: RecipeReadinessCheck
    observed_at: datetime.datetime
    readiness: RecipeReadinessCheck
    fit: None | SparkFit | Unset = UNSET
    group: None | SparkGroup | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_readiness_check import RecipeReadinessCheck # noqa: PLC0415
        from ..models.spark_fit import SparkFit # noqa: PLC0415
        from ..models.spark_group import SparkGroup # noqa: PLC0415
        cache = self.cache.to_dict()

        fleet_fit = self.fleet_fit.to_dict()

        observed_at = self.observed_at.isoformat()

        readiness = self.readiness.to_dict()

        fit: dict[str, Any] | None | Unset
        if isinstance(self.fit, Unset):
            fit = UNSET
        elif isinstance(self.fit, SparkFit):
            fit = self.fit.to_dict()
        else:
            fit = self.fit

        group: dict[str, Any] | None | Unset
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
        from ..models.recipe_readiness_check import RecipeReadinessCheck # noqa: PLC0415
        from ..models.spark_fit import SparkFit # noqa: PLC0415
        from ..models.spark_group import SparkGroup # noqa: PLC0415
        d = dict(src_dict)
        cache = RecipeReadinessCheck.from_dict(d.pop("cache"))




        fleet_fit = RecipeReadinessCheck.from_dict(d.pop("fleet_fit"))




        observed_at = datetime.datetime.fromisoformat(d.pop("observed_at"))




        readiness = RecipeReadinessCheck.from_dict(d.pop("readiness"))




        def _parse_fit(data: object) -> None | SparkFit | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                fit_type_0 = SparkFit.from_dict(data)



                return fit_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | SparkFit | Unset, data)

        fit = _parse_fit(d.pop("fit", UNSET))


        def _parse_group(data: object) -> None | SparkGroup | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                group_type_0 = SparkGroup.from_dict(data)



                return group_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | SparkGroup | Unset, data)

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
