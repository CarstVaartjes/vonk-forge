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
  from ..models.library_local_state import LibraryLocalState
  from ..models.library_recipe_identity import LibraryRecipeIdentity
  from ..models.library_resource_projection import LibraryResourceProjection
  from ..models.recipe_definition import RecipeDefinition
  from ..models.recipe_readiness import RecipeReadiness





T = TypeVar("T", bound="LibraryRecipeProjection")



@_attrs_define
class LibraryRecipeProjection:
    """ One exact canonical recipe and its model/resource/local projections.

        Attributes:
            document (RecipeDefinition): The sole public recipe authoring contract.
            engine (str):
            identity (LibraryRecipeIdentity):
            local (LibraryLocalState): Controller cache and Spark-local runtime evidence kept separate.
            model_selectors (list[str]):
            node_count (int):
            resources (LibraryResourceProjection): Declared resource facts; unknown values remain null.
            selector (str):
            updated_at (datetime.datetime):
            usage (list[str]):
            alignment (None | str | Unset):
            assessment (None | RecipeReadiness | Unset):
            creator (None | str | Unset):
     """

    document: RecipeDefinition
    engine: str
    identity: LibraryRecipeIdentity
    local: LibraryLocalState
    model_selectors: list[str]
    node_count: int
    resources: LibraryResourceProjection
    selector: str
    updated_at: datetime.datetime
    usage: list[str]
    alignment: None | str | Unset = UNSET
    assessment: None | RecipeReadiness | Unset = UNSET
    creator: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.library_local_state import LibraryLocalState # noqa: PLC0415
        from ..models.library_recipe_identity import LibraryRecipeIdentity # noqa: PLC0415
        from ..models.library_resource_projection import LibraryResourceProjection # noqa: PLC0415
        from ..models.recipe_definition import RecipeDefinition # noqa: PLC0415
        from ..models.recipe_readiness import RecipeReadiness # noqa: PLC0415
        document = self.document.to_dict()

        engine = self.engine

        identity = self.identity.to_dict()

        local = self.local.to_dict()

        model_selectors = self.model_selectors



        node_count = self.node_count

        resources = self.resources.to_dict()

        selector = self.selector

        updated_at = self.updated_at.isoformat()

        usage = self.usage



        alignment: None | str | Unset
        if isinstance(self.alignment, Unset):
            alignment = UNSET
        else:
            alignment = self.alignment

        assessment: dict[str, Any] | None | Unset
        if isinstance(self.assessment, Unset):
            assessment = UNSET
        elif isinstance(self.assessment, RecipeReadiness):
            assessment = self.assessment.to_dict()
        else:
            assessment = self.assessment

        creator: None | str | Unset
        if isinstance(self.creator, Unset):
            creator = UNSET
        else:
            creator = self.creator


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "document": document,
            "engine": engine,
            "identity": identity,
            "local": local,
            "model_selectors": model_selectors,
            "node_count": node_count,
            "resources": resources,
            "selector": selector,
            "updated_at": updated_at,
            "usage": usage,
        })
        if alignment is not UNSET:
            field_dict["alignment"] = alignment
        if assessment is not UNSET:
            field_dict["assessment"] = assessment
        if creator is not UNSET:
            field_dict["creator"] = creator

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.library_local_state import LibraryLocalState # noqa: PLC0415
        from ..models.library_recipe_identity import LibraryRecipeIdentity # noqa: PLC0415
        from ..models.library_resource_projection import LibraryResourceProjection # noqa: PLC0415
        from ..models.recipe_definition import RecipeDefinition # noqa: PLC0415
        from ..models.recipe_readiness import RecipeReadiness # noqa: PLC0415
        d = dict(src_dict)
        document = RecipeDefinition.from_dict(d.pop("document"))




        engine = d.pop("engine")

        identity = LibraryRecipeIdentity.from_dict(d.pop("identity"))




        local = LibraryLocalState.from_dict(d.pop("local"))




        model_selectors = cast(list[str], d.pop("model_selectors"))


        node_count = d.pop("node_count")

        resources = LibraryResourceProjection.from_dict(d.pop("resources"))




        selector = d.pop("selector")

        updated_at = datetime.datetime.fromisoformat(d.pop("updated_at"))




        usage = cast(list[str], d.pop("usage"))


        def _parse_alignment(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        alignment = _parse_alignment(d.pop("alignment", UNSET))


        def _parse_assessment(data: object) -> None | RecipeReadiness | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                assessment_type_0 = RecipeReadiness.from_dict(data)



                return assessment_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RecipeReadiness | Unset, data)

        assessment = _parse_assessment(d.pop("assessment", UNSET))


        def _parse_creator(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        creator = _parse_creator(d.pop("creator", UNSET))


        library_recipe_projection = cls(
            document=document,
            engine=engine,
            identity=identity,
            local=local,
            model_selectors=model_selectors,
            node_count=node_count,
            resources=resources,
            selector=selector,
            updated_at=updated_at,
            usage=usage,
            alignment=alignment,
            assessment=assessment,
            creator=creator,
        )

        return library_recipe_projection
