from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.library_recipe_summary_source_kind import check_library_recipe_summary_source_kind
from ..models.library_recipe_summary_source_kind import LibraryRecipeSummarySourceKind
from typing import cast
from typing import cast, Union

if TYPE_CHECKING:
  from ..models.recipe_profile_summary import RecipeProfileSummary
  from ..models.recipe_revision_summary import RecipeRevisionSummary
  from ..models.library_run_summary import LibraryRunSummary
  from ..models.library_installation_summary import LibraryInstallationSummary
  from ..models.library_projection_reason import LibraryProjectionReason





T = TypeVar("T", bound="LibraryRecipeSummary")



@_attrs_define
class LibraryRecipeSummary:
    """
        Attributes:
            capabilities (list[str]):
            description (str):
            installation_returned_count (int):
            installation_total_count (int):
            installations (list['LibraryInstallationSummary']):
            installations_truncated (bool):
            profiles (list['RecipeProfileSummary']):
            reasons (list['LibraryProjectionReason']):
            recipe_id (str):
            run_returned_count (int):
            run_total_count (int):
            runs (list['LibraryRunSummary']):
            runs_truncated (bool):
            selected_revision (Union['RecipeRevisionSummary', None]):
            slug (str):
            source_kind (LibraryRecipeSummarySourceKind):
            title (str):
     """

    capabilities: list[str]
    description: str
    installation_returned_count: int
    installation_total_count: int
    installations: list['LibraryInstallationSummary']
    installations_truncated: bool
    profiles: list['RecipeProfileSummary']
    reasons: list['LibraryProjectionReason']
    recipe_id: str
    run_returned_count: int
    run_total_count: int
    runs: list['LibraryRunSummary']
    runs_truncated: bool
    selected_revision: Union['RecipeRevisionSummary', None]
    slug: str
    source_kind: LibraryRecipeSummarySourceKind
    title: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_profile_summary import RecipeProfileSummary
        from ..models.recipe_revision_summary import RecipeRevisionSummary
        from ..models.library_run_summary import LibraryRunSummary
        from ..models.library_installation_summary import LibraryInstallationSummary
        from ..models.library_projection_reason import LibraryProjectionReason
        capabilities = self.capabilities



        description = self.description

        installation_returned_count = self.installation_returned_count

        installation_total_count = self.installation_total_count

        installations = []
        for installations_item_data in self.installations:
            installations_item = installations_item_data.to_dict()
            installations.append(installations_item)



        installations_truncated = self.installations_truncated

        profiles = []
        for profiles_item_data in self.profiles:
            profiles_item = profiles_item_data.to_dict()
            profiles.append(profiles_item)



        reasons = []
        for reasons_item_data in self.reasons:
            reasons_item = reasons_item_data.to_dict()
            reasons.append(reasons_item)



        recipe_id = self.recipe_id

        run_returned_count = self.run_returned_count

        run_total_count = self.run_total_count

        runs = []
        for runs_item_data in self.runs:
            runs_item = runs_item_data.to_dict()
            runs.append(runs_item)



        runs_truncated = self.runs_truncated

        selected_revision: Union[None, dict[str, Any]]
        if isinstance(self.selected_revision, RecipeRevisionSummary):
            selected_revision = self.selected_revision.to_dict()
        else:
            selected_revision = self.selected_revision

        slug = self.slug

        source_kind: str = self.source_kind

        title = self.title


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "capabilities": capabilities,
            "description": description,
            "installation_returned_count": installation_returned_count,
            "installation_total_count": installation_total_count,
            "installations": installations,
            "installations_truncated": installations_truncated,
            "profiles": profiles,
            "reasons": reasons,
            "recipe_id": recipe_id,
            "run_returned_count": run_returned_count,
            "run_total_count": run_total_count,
            "runs": runs,
            "runs_truncated": runs_truncated,
            "selected_revision": selected_revision,
            "slug": slug,
            "source_kind": source_kind,
            "title": title,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_profile_summary import RecipeProfileSummary
        from ..models.recipe_revision_summary import RecipeRevisionSummary
        from ..models.library_run_summary import LibraryRunSummary
        from ..models.library_installation_summary import LibraryInstallationSummary
        from ..models.library_projection_reason import LibraryProjectionReason
        d = dict(src_dict)
        capabilities = cast(list[str], d.pop("capabilities"))


        description = d.pop("description")

        installation_returned_count = d.pop("installation_returned_count")

        installation_total_count = d.pop("installation_total_count")

        installations = []
        _installations = d.pop("installations")
        for installations_item_data in (_installations):
            installations_item = LibraryInstallationSummary.from_dict(installations_item_data)



            installations.append(installations_item)


        installations_truncated = d.pop("installations_truncated")

        profiles = []
        _profiles = d.pop("profiles")
        for profiles_item_data in (_profiles):
            profiles_item = RecipeProfileSummary.from_dict(profiles_item_data)



            profiles.append(profiles_item)


        reasons = []
        _reasons = d.pop("reasons")
        for reasons_item_data in (_reasons):
            reasons_item = LibraryProjectionReason.from_dict(reasons_item_data)



            reasons.append(reasons_item)


        recipe_id = d.pop("recipe_id")

        run_returned_count = d.pop("run_returned_count")

        run_total_count = d.pop("run_total_count")

        runs = []
        _runs = d.pop("runs")
        for runs_item_data in (_runs):
            runs_item = LibraryRunSummary.from_dict(runs_item_data)



            runs.append(runs_item)


        runs_truncated = d.pop("runs_truncated")

        def _parse_selected_revision(data: object) -> Union['RecipeRevisionSummary', None]:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                selected_revision_type_0 = RecipeRevisionSummary.from_dict(data)



                return selected_revision_type_0
            except: # noqa: E722
                pass
            return cast(Union['RecipeRevisionSummary', None], data)

        selected_revision = _parse_selected_revision(d.pop("selected_revision"))


        slug = d.pop("slug")

        source_kind = check_library_recipe_summary_source_kind(d.pop("source_kind"))




        title = d.pop("title")

        library_recipe_summary = cls(
            capabilities=capabilities,
            description=description,
            installation_returned_count=installation_returned_count,
            installation_total_count=installation_total_count,
            installations=installations,
            installations_truncated=installations_truncated,
            profiles=profiles,
            reasons=reasons,
            recipe_id=recipe_id,
            run_returned_count=run_returned_count,
            run_total_count=run_total_count,
            runs=runs,
            runs_truncated=runs_truncated,
            selected_revision=selected_revision,
            slug=slug,
            source_kind=source_kind,
            title=title,
        )

        return library_recipe_summary
