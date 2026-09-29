from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_alternative_cache import check_recipe_alternative_cache
from ..models.recipe_alternative_cache import RecipeAlternativeCache
from ..models.recipe_alternative_fits_fleet import check_recipe_alternative_fits_fleet
from ..models.recipe_alternative_fits_fleet import RecipeAlternativeFitsFleet
from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="RecipeAlternative")



@_attrs_define
class RecipeAlternative:
    """ One other recipe serving the same model, for a one-line comparison.

        Attributes:
            cache (RecipeAlternativeCache):
            engine (str):
            fits_fleet (RecipeAlternativeFitsFleet):
            node_count (int):
            selector (str):
            title (str):
            version (str):
            creator (None | str | Unset):
     """

    cache: RecipeAlternativeCache
    engine: str
    fits_fleet: RecipeAlternativeFitsFleet
    node_count: int
    selector: str
    title: str
    version: str
    creator: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        cache: str = self.cache

        engine = self.engine

        fits_fleet: str = self.fits_fleet

        node_count = self.node_count

        selector = self.selector

        title = self.title

        version = self.version

        creator: None | str | Unset
        if isinstance(self.creator, Unset):
            creator = UNSET
        else:
            creator = self.creator


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "cache": cache,
            "engine": engine,
            "fits_fleet": fits_fleet,
            "node_count": node_count,
            "selector": selector,
            "title": title,
            "version": version,
        })
        if creator is not UNSET:
            field_dict["creator"] = creator

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        cache = check_recipe_alternative_cache(d.pop("cache"))




        engine = d.pop("engine")

        fits_fleet = check_recipe_alternative_fits_fleet(d.pop("fits_fleet"))




        node_count = d.pop("node_count")

        selector = d.pop("selector")

        title = d.pop("title")

        version = d.pop("version")

        def _parse_creator(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        creator = _parse_creator(d.pop("creator", UNSET))


        recipe_alternative = cls(
            cache=cache,
            engine=engine,
            fits_fleet=fits_fleet,
            node_count=node_count,
            selector=selector,
            title=title,
            version=version,
            creator=creator,
        )

        return recipe_alternative
