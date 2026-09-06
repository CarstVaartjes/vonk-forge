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
  from ..models.library_capability_inventory import LibraryCapabilityInventory
  from ..models.library_run_summary import LibraryRunSummary
  from ..models.library_installation_summary import LibraryInstallationSummary
  from ..models.recipe_definition import RecipeDefinition
  from ..models.library_projection_reason import LibraryProjectionReason





T = TypeVar("T", bound="LibraryRecipeSummary")



@_attrs_define
class LibraryRecipeSummary:
    """
        Attributes:
            capabilities (list[str]):
            content_sha256 (str):
            description (str):
            installation_returned_count (int):
            installation_total_count (int):
            installations (list['LibraryInstallationSummary']):
            installations_truncated (bool):
            publisher (str):
            reasons (list['LibraryProjectionReason']):
            recipe_document (RecipeDefinition): The sole public recipe authoring contract.
            recipe_id (str):
            recipe_revision_id (str):
            run_returned_count (int):
            run_total_count (int):
            runs (list['LibraryRunSummary']):
            runs_truncated (bool):
            slug (str):
            title (str):
            topology_name (Union[None, str]):
            recipe_capabilities (Union[Unset, LibraryCapabilityInventory]): Compare-friendly model or recipe capability
                assertions with evidence state.
     """

    capabilities: list[str]
    content_sha256: str
    description: str
    installation_returned_count: int
    installation_total_count: int
    installations: list['LibraryInstallationSummary']
    installations_truncated: bool
    publisher: str
    reasons: list['LibraryProjectionReason']
    recipe_document: 'RecipeDefinition'
    recipe_id: str
    recipe_revision_id: str
    run_returned_count: int
    run_total_count: int
    runs: list['LibraryRunSummary']
    runs_truncated: bool
    slug: str
    title: str
    topology_name: Union[None, str]
    recipe_capabilities: Union[Unset, 'LibraryCapabilityInventory'] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.library_capability_inventory import LibraryCapabilityInventory
        from ..models.library_run_summary import LibraryRunSummary
        from ..models.library_installation_summary import LibraryInstallationSummary
        from ..models.recipe_definition import RecipeDefinition
        from ..models.library_projection_reason import LibraryProjectionReason
        capabilities = self.capabilities



        content_sha256 = self.content_sha256

        description = self.description

        installation_returned_count = self.installation_returned_count

        installation_total_count = self.installation_total_count

        installations = []
        for installations_item_data in self.installations:
            installations_item = installations_item_data.to_dict()
            installations.append(installations_item)



        installations_truncated = self.installations_truncated

        publisher = self.publisher

        reasons = []
        for reasons_item_data in self.reasons:
            reasons_item = reasons_item_data.to_dict()
            reasons.append(reasons_item)



        recipe_document = self.recipe_document.to_dict()

        recipe_id = self.recipe_id

        recipe_revision_id = self.recipe_revision_id

        run_returned_count = self.run_returned_count

        run_total_count = self.run_total_count

        runs = []
        for runs_item_data in self.runs:
            runs_item = runs_item_data.to_dict()
            runs.append(runs_item)



        runs_truncated = self.runs_truncated

        slug = self.slug

        title = self.title

        topology_name: Union[None, str]
        topology_name = self.topology_name

        recipe_capabilities: Union[Unset, dict[str, Any]] = UNSET
        if not isinstance(self.recipe_capabilities, Unset):
            recipe_capabilities = self.recipe_capabilities.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "capabilities": capabilities,
            "content_sha256": content_sha256,
            "description": description,
            "installation_returned_count": installation_returned_count,
            "installation_total_count": installation_total_count,
            "installations": installations,
            "installations_truncated": installations_truncated,
            "publisher": publisher,
            "reasons": reasons,
            "recipe_document": recipe_document,
            "recipe_id": recipe_id,
            "recipe_revision_id": recipe_revision_id,
            "run_returned_count": run_returned_count,
            "run_total_count": run_total_count,
            "runs": runs,
            "runs_truncated": runs_truncated,
            "slug": slug,
            "title": title,
            "topology_name": topology_name,
        })
        if recipe_capabilities is not UNSET:
            field_dict["recipe_capabilities"] = recipe_capabilities

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.library_capability_inventory import LibraryCapabilityInventory
        from ..models.library_run_summary import LibraryRunSummary
        from ..models.library_installation_summary import LibraryInstallationSummary
        from ..models.recipe_definition import RecipeDefinition
        from ..models.library_projection_reason import LibraryProjectionReason
        d = dict(src_dict)
        capabilities = cast(list[str], d.pop("capabilities"))


        content_sha256 = d.pop("content_sha256")

        description = d.pop("description")

        installation_returned_count = d.pop("installation_returned_count")

        installation_total_count = d.pop("installation_total_count")

        installations = []
        _installations = d.pop("installations")
        for installations_item_data in (_installations):
            installations_item = LibraryInstallationSummary.from_dict(installations_item_data)



            installations.append(installations_item)


        installations_truncated = d.pop("installations_truncated")

        publisher = d.pop("publisher")

        reasons = []
        _reasons = d.pop("reasons")
        for reasons_item_data in (_reasons):
            reasons_item = LibraryProjectionReason.from_dict(reasons_item_data)



            reasons.append(reasons_item)


        recipe_document = RecipeDefinition.from_dict(d.pop("recipe_document"))




        recipe_id = d.pop("recipe_id")

        recipe_revision_id = d.pop("recipe_revision_id")

        run_returned_count = d.pop("run_returned_count")

        run_total_count = d.pop("run_total_count")

        runs = []
        _runs = d.pop("runs")
        for runs_item_data in (_runs):
            runs_item = LibraryRunSummary.from_dict(runs_item_data)



            runs.append(runs_item)


        runs_truncated = d.pop("runs_truncated")

        slug = d.pop("slug")

        title = d.pop("title")

        def _parse_topology_name(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        topology_name = _parse_topology_name(d.pop("topology_name"))


        _recipe_capabilities = d.pop("recipe_capabilities", UNSET)
        recipe_capabilities: Union[Unset, LibraryCapabilityInventory]
        if isinstance(_recipe_capabilities,  Unset):
            recipe_capabilities = UNSET
        else:
            recipe_capabilities = LibraryCapabilityInventory.from_dict(_recipe_capabilities)




        library_recipe_summary = cls(
            capabilities=capabilities,
            content_sha256=content_sha256,
            description=description,
            installation_returned_count=installation_returned_count,
            installation_total_count=installation_total_count,
            installations=installations,
            installations_truncated=installations_truncated,
            publisher=publisher,
            reasons=reasons,
            recipe_document=recipe_document,
            recipe_id=recipe_id,
            recipe_revision_id=recipe_revision_id,
            run_returned_count=run_returned_count,
            run_total_count=run_total_count,
            runs=runs,
            runs_truncated=runs_truncated,
            slug=slug,
            title=title,
            topology_name=topology_name,
            recipe_capabilities=recipe_capabilities,
        )

        return library_recipe_summary
