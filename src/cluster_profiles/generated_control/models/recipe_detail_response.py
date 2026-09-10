from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast
from typing import Union
import datetime

if TYPE_CHECKING:
  from ..models.library_recipe_identity import LibraryRecipeIdentity
  from ..models.library_recipe_model import LibraryRecipeModel
  from ..models.library_resource_projection import LibraryResourceProjection
  from ..models.library_local_state import LibraryLocalState
  from ..models.recipe_definition import RecipeDefinition





T = TypeVar("T", bound="RecipeDetailResponse")



@_attrs_define
class RecipeDetailResponse:
    """
        Attributes:
            document (RecipeDefinition): The sole public recipe authoring contract.
            identity (LibraryRecipeIdentity):
            local (LibraryLocalState): Controller cache and Spark-local runtime evidence kept separate.
            model_documents (list['LibraryRecipeModel']):
            model_selectors (list[str]):
            node_count (int):
            resources (LibraryResourceProjection): Declared resource facts; unknown values remain null.
            selector (str):
            updated_at (datetime.datetime):
            usage (list[str]):
            alignment (Union[None, Unset, str]):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    document: 'RecipeDefinition'
    identity: 'LibraryRecipeIdentity'
    local: 'LibraryLocalState'
    model_documents: list['LibraryRecipeModel']
    model_selectors: list[str]
    node_count: int
    resources: 'LibraryResourceProjection'
    selector: str
    updated_at: datetime.datetime
    usage: list[str]
    alignment: Union[None, Unset, str] = UNSET
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.library_recipe_identity import LibraryRecipeIdentity
        from ..models.library_recipe_model import LibraryRecipeModel
        from ..models.library_resource_projection import LibraryResourceProjection
        from ..models.library_local_state import LibraryLocalState
        from ..models.recipe_definition import RecipeDefinition
        document = self.document.to_dict()

        identity = self.identity.to_dict()

        local = self.local.to_dict()

        model_documents = []
        for model_documents_item_data in self.model_documents:
            model_documents_item = model_documents_item_data.to_dict()
            model_documents.append(model_documents_item)



        model_selectors = self.model_selectors



        node_count = self.node_count

        resources = self.resources.to_dict()

        selector = self.selector

        updated_at = self.updated_at.isoformat()

        usage = self.usage



        alignment: Union[None, Unset, str]
        if isinstance(self.alignment, Unset):
            alignment = UNSET
        else:
            alignment = self.alignment

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "document": document,
            "identity": identity,
            "local": local,
            "model_documents": model_documents,
            "model_selectors": model_selectors,
            "node_count": node_count,
            "resources": resources,
            "selector": selector,
            "updated_at": updated_at,
            "usage": usage,
        })
        if alignment is not UNSET:
            field_dict["alignment"] = alignment
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.library_recipe_identity import LibraryRecipeIdentity
        from ..models.library_recipe_model import LibraryRecipeModel
        from ..models.library_resource_projection import LibraryResourceProjection
        from ..models.library_local_state import LibraryLocalState
        from ..models.recipe_definition import RecipeDefinition
        d = dict(src_dict)
        document = RecipeDefinition.from_dict(d.pop("document"))




        identity = LibraryRecipeIdentity.from_dict(d.pop("identity"))




        local = LibraryLocalState.from_dict(d.pop("local"))




        model_documents = []
        _model_documents = d.pop("model_documents")
        for model_documents_item_data in (_model_documents):
            model_documents_item = LibraryRecipeModel.from_dict(model_documents_item_data)



            model_documents.append(model_documents_item)


        model_selectors = cast(list[str], d.pop("model_selectors"))


        node_count = d.pop("node_count")

        resources = LibraryResourceProjection.from_dict(d.pop("resources"))




        selector = d.pop("selector")

        updated_at = isoparse(d.pop("updated_at"))




        usage = cast(list[str], d.pop("usage"))


        def _parse_alignment(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        alignment = _parse_alignment(d.pop("alignment", UNSET))


        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        recipe_detail_response = cls(
            document=document,
            identity=identity,
            local=local,
            model_documents=model_documents,
            model_selectors=model_selectors,
            node_count=node_count,
            resources=resources,
            selector=selector,
            updated_at=updated_at,
            usage=usage,
            alignment=alignment,
            schema_version=schema_version,
        )

        return recipe_detail_response
