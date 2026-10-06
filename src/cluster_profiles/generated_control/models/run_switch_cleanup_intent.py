from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.run_switch_cleanup_intent_cleanup_mode import check_run_switch_cleanup_intent_cleanup_mode
from ..models.run_switch_cleanup_intent_cleanup_mode import RunSwitchCleanupIntentCleanupMode
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="RunSwitchCleanupIntent")



@_attrs_define
class RunSwitchCleanupIntent:
    """
        Attributes:
            installation_id (str):
            type_ (Literal['cleanup']):
            cleanup_mode (RunSwitchCleanupIntentCleanupMode | Unset):  Default: 'uninstall'.
            plan_digest (None | str | Unset):
            request_key (None | str | Unset):
     """

    installation_id: str
    type_: Literal['cleanup']
    cleanup_mode: RunSwitchCleanupIntentCleanupMode | Unset = 'uninstall'
    plan_digest: None | str | Unset = UNSET
    request_key: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        installation_id = self.installation_id

        type_ = self.type_

        cleanup_mode: str | Unset = UNSET
        if not isinstance(self.cleanup_mode, Unset):
            cleanup_mode = self.cleanup_mode


        plan_digest: None | str | Unset
        if isinstance(self.plan_digest, Unset):
            plan_digest = UNSET
        else:
            plan_digest = self.plan_digest

        request_key: None | str | Unset
        if isinstance(self.request_key, Unset):
            request_key = UNSET
        else:
            request_key = self.request_key


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "installation_id": installation_id,
            "type": type_,
        })
        if cleanup_mode is not UNSET:
            field_dict["cleanup_mode"] = cleanup_mode
        if plan_digest is not UNSET:
            field_dict["plan_digest"] = plan_digest
        if request_key is not UNSET:
            field_dict["request_key"] = request_key

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        installation_id = d.pop("installation_id")

        type_ = cast(Literal['cleanup'] , d.pop("type"))
        if type_ != 'cleanup':
            raise ValueError(f"type must match const 'cleanup', got '{type_}'")

        _cleanup_mode = d.pop("cleanup_mode", UNSET)
        cleanup_mode: RunSwitchCleanupIntentCleanupMode | Unset
        if isinstance(_cleanup_mode,  Unset):
            cleanup_mode = UNSET
        else:
            cleanup_mode = check_run_switch_cleanup_intent_cleanup_mode(_cleanup_mode)




        def _parse_plan_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        plan_digest = _parse_plan_digest(d.pop("plan_digest", UNSET))


        def _parse_request_key(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        request_key = _parse_request_key(d.pop("request_key", UNSET))


        run_switch_cleanup_intent = cls(
            installation_id=installation_id,
            type_=type_,
            cleanup_mode=cleanup_mode,
            plan_digest=plan_digest,
            request_key=request_key,
        )

        return run_switch_cleanup_intent
