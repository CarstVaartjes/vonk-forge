from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.operation_recovery_action import check_operation_recovery_action
from ..models.operation_recovery_action import OperationRecoveryAction
from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="OperationRecovery")



@_attrs_define
class OperationRecovery:
    """
        Attributes:
            actions (list[OperationRecoveryAction] | Unset):
            explanation (None | str | Unset):
            uncertain (bool | Unset):  Default: False.
     """

    actions: list[OperationRecoveryAction] | Unset = UNSET
    explanation: None | str | Unset = UNSET
    uncertain: bool | Unset = False





    def to_dict(self) -> dict[str, Any]:
        actions: list[str] | Unset = UNSET
        if not isinstance(self.actions, Unset):
            actions = []
            for actions_item_data in self.actions:
                actions_item: str = actions_item_data
                actions.append(actions_item)



        explanation: None | str | Unset
        if isinstance(self.explanation, Unset):
            explanation = UNSET
        else:
            explanation = self.explanation

        uncertain = self.uncertain


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if actions is not UNSET:
            field_dict["actions"] = actions
        if explanation is not UNSET:
            field_dict["explanation"] = explanation
        if uncertain is not UNSET:
            field_dict["uncertain"] = uncertain

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        _actions = d.pop("actions", UNSET)
        actions: list[OperationRecoveryAction] | Unset = UNSET
        if _actions is not UNSET:
            actions = []
            for actions_item_data in _actions:
                actions_item = check_operation_recovery_action(actions_item_data)



                actions.append(actions_item)


        def _parse_explanation(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        explanation = _parse_explanation(d.pop("explanation", UNSET))


        uncertain = d.pop("uncertain", UNSET)

        operation_recovery = cls(
            actions=actions,
            explanation=explanation,
            uncertain=uncertain,
        )

        return operation_recovery
