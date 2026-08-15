from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_revision_summary_lifecycle import check_recipe_revision_summary_lifecycle
from ..models.recipe_revision_summary_lifecycle import RecipeRevisionSummaryLifecycle
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
import datetime






T = TypeVar("T", bound="RecipeRevisionSummary")



@_attrs_define
class RecipeRevisionSummary:
    """
        Attributes:
            content_sha256 (Union[None, str]):
            created_at (datetime.datetime):
            id (str):
            lifecycle (RecipeRevisionSummaryLifecycle):
            revision_number (int):
            schema_version (int):
     """

    content_sha256: Union[None, str]
    created_at: datetime.datetime
    id: str
    lifecycle: RecipeRevisionSummaryLifecycle
    revision_number: int
    schema_version: int





    def to_dict(self) -> dict[str, Any]:
        content_sha256: Union[None, str]
        content_sha256 = self.content_sha256

        created_at = self.created_at.isoformat()

        id = self.id

        lifecycle: str = self.lifecycle

        revision_number = self.revision_number

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "content_sha256": content_sha256,
            "created_at": created_at,
            "id": id,
            "lifecycle": lifecycle,
            "revision_number": revision_number,
            "schema_version": schema_version,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        def _parse_content_sha256(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        content_sha256 = _parse_content_sha256(d.pop("content_sha256"))


        created_at = isoparse(d.pop("created_at"))




        id = d.pop("id")

        lifecycle = check_recipe_revision_summary_lifecycle(d.pop("lifecycle"))




        revision_number = d.pop("revision_number")

        schema_version = d.pop("schema_version")

        recipe_revision_summary = cls(
            content_sha256=content_sha256,
            created_at=created_at,
            id=id,
            lifecycle=lifecycle,
            revision_number=revision_number,
            schema_version=schema_version,
        )

        return recipe_revision_summary
