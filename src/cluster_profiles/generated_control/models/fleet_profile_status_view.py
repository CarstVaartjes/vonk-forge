from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_status_view_state import check_fleet_profile_status_view_state
from ..models.fleet_profile_status_view_state import FleetProfileStatusViewState
from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import Literal, Union, cast
import datetime

if TYPE_CHECKING:
  from ..models.fleet_profile_scope_preview import FleetProfileScopePreview
  from ..models.fleet_profile_reason import FleetProfileReason





T = TypeVar("T", bound="FleetProfileStatusView")



@_attrs_define
class FleetProfileStatusView:
    """
        Attributes:
            drifted (bool):
            generated_at (datetime.datetime):
            matched (bool):
            profile_digest (str):
            profile_id (str):
            reasons (list['FleetProfileReason']):
            scope (FleetProfileScopePreview):
            state (FleetProfileStatusViewState):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    drifted: bool
    generated_at: datetime.datetime
    matched: bool
    profile_digest: str
    profile_id: str
    reasons: list['FleetProfileReason']
    scope: 'FleetProfileScopePreview'
    state: FleetProfileStatusViewState
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_scope_preview import FleetProfileScopePreview
        from ..models.fleet_profile_reason import FleetProfileReason
        drifted = self.drifted

        generated_at = self.generated_at.isoformat()

        matched = self.matched

        profile_digest = self.profile_digest

        profile_id = self.profile_id

        reasons = []
        for reasons_item_data in self.reasons:
            reasons_item = reasons_item_data.to_dict()
            reasons.append(reasons_item)



        scope = self.scope.to_dict()

        state: str = self.state

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "drifted": drifted,
            "generated_at": generated_at,
            "matched": matched,
            "profile_digest": profile_digest,
            "profile_id": profile_id,
            "reasons": reasons,
            "scope": scope,
            "state": state,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_scope_preview import FleetProfileScopePreview
        from ..models.fleet_profile_reason import FleetProfileReason
        d = dict(src_dict)
        drifted = d.pop("drifted")

        generated_at = isoparse(d.pop("generated_at"))




        matched = d.pop("matched")

        profile_digest = d.pop("profile_digest")

        profile_id = d.pop("profile_id")

        reasons = []
        _reasons = d.pop("reasons")
        for reasons_item_data in (_reasons):
            reasons_item = FleetProfileReason.from_dict(reasons_item_data)



            reasons.append(reasons_item)


        scope = FleetProfileScopePreview.from_dict(d.pop("scope"))




        state = check_fleet_profile_status_view_state(d.pop("state"))




        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        fleet_profile_status_view = cls(
            drifted=drifted,
            generated_at=generated_at,
            matched=matched,
            profile_digest=profile_digest,
            profile_id=profile_id,
            reasons=reasons,
            scope=scope,
            state=state,
            schema_version=schema_version,
        )

        return fleet_profile_status_view
