from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import Literal, Union, cast
import datetime

if TYPE_CHECKING:
  from ..models.library_recipe_identity import LibraryRecipeIdentity
  from ..models.library_local_state import LibraryLocalState
  from ..models.recipe_definition import RecipeDefinition
  from ..models.library_resource_projection import LibraryResourceProjection





T = TypeVar("T", bound="LibraryRecipeProjection")



@_attrs_define
class LibraryRecipeProjection:
    """ One exact canonical recipe and its model/resource/local projections.

        Attributes:
            document (RecipeDefinition): The sole public recipe authoring contract.
            identity (LibraryRecipeIdentity):
            local (LibraryLocalState): Controller cache and Spark-local runtime evidence kept separate.
            model_selectors (list[str]):
            resources (LibraryResourceProjection): Declared resource facts; unknown values remain null.
            selector (str):
            updated_at (datetime.datetime):
            usage (list[str]):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    document: 'RecipeDefinition'
    identity: 'LibraryRecipeIdentity'
    local: 'LibraryLocalState'
    model_selectors: list[str]
    resources: 'LibraryResourceProjection'
    selector: str
    updated_at: datetime.datetime
    usage: list[str]
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.library_recipe_identity import LibraryRecipeIdentity
        from ..models.library_local_state import LibraryLocalState
        from ..models.recipe_definition import RecipeDefinition
        from ..models.library_resource_projection import LibraryResourceProjection
        document = self.document.to_dict()

        identity = self.identity.to_dict()

        local = self.local.to_dict()

        model_selectors = self.model_selectors



        resources = self.resources.to_dict()

        selector = self.selector

        updated_at = self.updated_at.isoformat()

        usage = self.usage



        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "document": document,
            "identity": identity,
            "local": local,
            "model_selectors": model_selectors,
            "resources": resources,
            "selector": selector,
            "updated_at": updated_at,
            "usage": usage,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.library_recipe_identity import LibraryRecipeIdentity
        from ..models.library_local_state import LibraryLocalState
        from ..models.recipe_definition import RecipeDefinition
        from ..models.library_resource_projection import LibraryResourceProjection
        d = dict(src_dict)
        document = RecipeDefinition.from_dict(d.pop("document"))




        identity = LibraryRecipeIdentity.from_dict(d.pop("identity"))




        local = LibraryLocalState.from_dict(d.pop("local"))




        model_selectors = cast(list[str], d.pop("model_selectors"))


        resources = LibraryResourceProjection.from_dict(d.pop("resources"))




        selector = d.pop("selector")

        updated_at = isoparse(d.pop("updated_at"))




        usage = cast(list[str], d.pop("usage"))


        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        library_recipe_projection = cls(
            document=document,
            identity=identity,
            local=local,
            model_selectors=model_selectors,
            resources=resources,
            selector=selector,
            updated_at=updated_at,
            usage=usage,
            schema_version=schema_version,
        )

        return library_recipe_projection
