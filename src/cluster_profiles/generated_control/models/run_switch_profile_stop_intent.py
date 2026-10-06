from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.run_switch_profile_stop_scope import RunSwitchProfileStopScope





T = TypeVar("T", bound="RunSwitchProfileStopIntent")



@_attrs_define
class RunSwitchProfileStopIntent:
    """
        Attributes:
            profile_stop_scope (RunSwitchProfileStopScope): Reviewed profile-only cleanup of the reachable ranks in a lost
                group.

                The full accepted topology remains visible even though only its reachable
                subset is sent Stop work.  Missing ranks are explicit so a partial cleanup
                can never be presented as a successful full-group stop.
            run_id (str):
            type_ (Literal['profile-stop']):
     """

    profile_stop_scope: RunSwitchProfileStopScope
    run_id: str
    type_: Literal['profile-stop']





    def to_dict(self) -> dict[str, Any]:
        from ..models.run_switch_profile_stop_scope import RunSwitchProfileStopScope # noqa: PLC0415
        profile_stop_scope = self.profile_stop_scope.to_dict()

        run_id = self.run_id

        type_ = self.type_


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "profile_stop_scope": profile_stop_scope,
            "run_id": run_id,
            "type": type_,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.run_switch_profile_stop_scope import RunSwitchProfileStopScope # noqa: PLC0415
        d = dict(src_dict)
        profile_stop_scope = RunSwitchProfileStopScope.from_dict(d.pop("profile_stop_scope"))




        run_id = d.pop("run_id")

        type_ = cast(Literal['profile-stop'] , d.pop("type"))
        if type_ != 'profile-stop':
            raise ValueError(f"type must match const 'profile-stop', got '{type_}'")

        run_switch_profile_stop_intent = cls(
            profile_stop_scope=profile_stop_scope,
            run_id=run_id,
            type_=type_,
        )

        return run_switch_profile_stop_intent
