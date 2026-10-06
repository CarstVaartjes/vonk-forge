from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.saved_profile_projection_issue import SavedProfileProjectionIssue





T = TypeVar("T", bound="UnavailableFleetProfileView")



@_attrs_define
class UnavailableFleetProfileView:
    """ Keep an authorized saved identity visible without inventing its contents.

        Attributes:
            id (str):
            number (int):
            projection_issue (SavedProfileProjectionIssue): An observation problem; it never authorizes changing saved
                intent.
            revision (int):
            definition (None | Unset):
            status (Literal['unavailable'] | Unset):  Default: 'unavailable'.
     """

    id: str
    number: int
    projection_issue: SavedProfileProjectionIssue
    revision: int
    definition: None | Unset = UNSET
    status: Literal['unavailable'] | Unset = 'unavailable'





    def to_dict(self) -> dict[str, Any]:
        from ..models.saved_profile_projection_issue import SavedProfileProjectionIssue # noqa: PLC0415
        id = self.id

        number = self.number

        projection_issue = self.projection_issue.to_dict()

        revision = self.revision

        definition = self.definition

        status = self.status


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "id": id,
            "number": number,
            "projection_issue": projection_issue,
            "revision": revision,
        })
        if definition is not UNSET:
            field_dict["definition"] = definition
        if status is not UNSET:
            field_dict["status"] = status

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.saved_profile_projection_issue import SavedProfileProjectionIssue # noqa: PLC0415
        d = dict(src_dict)
        id = d.pop("id")

        number = d.pop("number")

        projection_issue = SavedProfileProjectionIssue.from_dict(d.pop("projection_issue"))




        revision = d.pop("revision")

        definition = d.pop("definition", UNSET)

        status = cast(Literal['unavailable'] | Unset , d.pop("status", UNSET))
        if status != 'unavailable' and not isinstance(status, Unset):
            raise ValueError(f"status must match const 'unavailable', got '{status}'")

        unavailable_fleet_profile_view = cls(
            id=id,
            number=number,
            projection_issue=projection_issue,
            revision=revision,
            definition=definition,
            status=status,
        )

        return unavailable_fleet_profile_view
