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
  from ..models.agent_upgrade_target_diagnostics_response import AgentUpgradeTargetDiagnosticsResponse
  from ..models.agent_upgrade_identity_response import AgentUpgradeIdentityResponse





T = TypeVar("T", bound="AgentUpgradeDiagnosticsResponse")



@_attrs_define
class AgentUpgradeDiagnosticsResponse:
    """
        Attributes:
            expected_identity (AgentUpgradeIdentityResponse):
            legacy_generic_ambiguous (bool):
            targets (list['AgentUpgradeTargetDiagnosticsResponse']):
            next_action (Union[None, Unset, str]):
            operator_summary (Union[None, Unset, str]):
     """

    expected_identity: 'AgentUpgradeIdentityResponse'
    legacy_generic_ambiguous: bool
    targets: list['AgentUpgradeTargetDiagnosticsResponse']
    next_action: Union[None, Unset, str] = UNSET
    operator_summary: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.agent_upgrade_target_diagnostics_response import AgentUpgradeTargetDiagnosticsResponse
        from ..models.agent_upgrade_identity_response import AgentUpgradeIdentityResponse
        expected_identity = self.expected_identity.to_dict()

        legacy_generic_ambiguous = self.legacy_generic_ambiguous

        targets = []
        for targets_item_data in self.targets:
            targets_item = targets_item_data.to_dict()
            targets.append(targets_item)



        next_action: Union[None, Unset, str]
        if isinstance(self.next_action, Unset):
            next_action = UNSET
        else:
            next_action = self.next_action

        operator_summary: Union[None, Unset, str]
        if isinstance(self.operator_summary, Unset):
            operator_summary = UNSET
        else:
            operator_summary = self.operator_summary


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "expected_identity": expected_identity,
            "legacy_generic_ambiguous": legacy_generic_ambiguous,
            "targets": targets,
        })
        if next_action is not UNSET:
            field_dict["next_action"] = next_action
        if operator_summary is not UNSET:
            field_dict["operator_summary"] = operator_summary

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.agent_upgrade_target_diagnostics_response import AgentUpgradeTargetDiagnosticsResponse
        from ..models.agent_upgrade_identity_response import AgentUpgradeIdentityResponse
        d = dict(src_dict)
        expected_identity = AgentUpgradeIdentityResponse.from_dict(d.pop("expected_identity"))




        legacy_generic_ambiguous = d.pop("legacy_generic_ambiguous")

        targets = []
        _targets = d.pop("targets")
        for targets_item_data in (_targets):
            targets_item = AgentUpgradeTargetDiagnosticsResponse.from_dict(targets_item_data)



            targets.append(targets_item)


        def _parse_next_action(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        next_action = _parse_next_action(d.pop("next_action", UNSET))


        def _parse_operator_summary(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        operator_summary = _parse_operator_summary(d.pop("operator_summary", UNSET))


        agent_upgrade_diagnostics_response = cls(
            expected_identity=expected_identity,
            legacy_generic_ambiguous=legacy_generic_ambiguous,
            targets=targets,
            next_action=next_action,
            operator_summary=operator_summary,
        )

        return agent_upgrade_diagnostics_response
