from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.operation_recovery_action import check_operation_recovery_action
from ..models.operation_recovery_action import OperationRecoveryAction
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="OperationRecovery")



@_attrs_define
class OperationRecovery:
    """
        Attributes:
            actions (Union[Unset, list[OperationRecoveryAction]]):
            explanation (Union[None, Unset, str]):
            uncertain (Union[Unset, bool]):  Default: False.
     """

    actions: Union[Unset, list[OperationRecoveryAction]] = UNSET
    explanation: Union[None, Unset, str] = UNSET
    uncertain: Union[Unset, bool] = False





    def to_dict(self) -> dict[str, Any]:
        actions: Union[Unset, list[str]] = UNSET
        if not isinstance(self.actions, Unset):
            actions = []
            for actions_item_data in self.actions:
                actions_item: str = actions_item_data
                actions.append(actions_item)



        explanation: Union[None, Unset, str]
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
        actions = []
        _actions = d.pop("actions", UNSET)
        for actions_item_data in (_actions or []):
            actions_item = check_operation_recovery_action(actions_item_data)



            actions.append(actions_item)


        def _parse_explanation(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        explanation = _parse_explanation(d.pop("explanation", UNSET))


        uncertain = d.pop("uncertain", UNSET)

        operation_recovery = cls(
            actions=actions,
            explanation=explanation,
            uncertain=uncertain,
        )

        return operation_recovery
