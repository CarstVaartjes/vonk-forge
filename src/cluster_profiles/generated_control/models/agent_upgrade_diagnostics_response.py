from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.agent_upgrade_identity_response import AgentUpgradeIdentityResponse
  from ..models.agent_upgrade_target_diagnostics_response import AgentUpgradeTargetDiagnosticsResponse





T = TypeVar("T", bound="AgentUpgradeDiagnosticsResponse")



@_attrs_define
class AgentUpgradeDiagnosticsResponse:
    """
        Attributes:
            expected_identity (AgentUpgradeIdentityResponse):
            failure_details_unavailable (bool):
            targets (list[AgentUpgradeTargetDiagnosticsResponse]):
            next_action (None | str | Unset):
            operator_summary (None | str | Unset):
     """

    expected_identity: AgentUpgradeIdentityResponse
    failure_details_unavailable: bool
    targets: list[AgentUpgradeTargetDiagnosticsResponse]
    next_action: None | str | Unset = UNSET
    operator_summary: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.agent_upgrade_identity_response import AgentUpgradeIdentityResponse # noqa: PLC0415
        from ..models.agent_upgrade_target_diagnostics_response import AgentUpgradeTargetDiagnosticsResponse # noqa: PLC0415
        expected_identity = self.expected_identity.to_dict()

        failure_details_unavailable = self.failure_details_unavailable

        targets = []
        for targets_item_data in self.targets:
            targets_item = targets_item_data.to_dict()
            targets.append(targets_item)



        next_action: None | str | Unset
        if isinstance(self.next_action, Unset):
            next_action = UNSET
        else:
            next_action = self.next_action

        operator_summary: None | str | Unset
        if isinstance(self.operator_summary, Unset):
            operator_summary = UNSET
        else:
            operator_summary = self.operator_summary


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "expected_identity": expected_identity,
            "failure_details_unavailable": failure_details_unavailable,
            "targets": targets,
        })
        if next_action is not UNSET:
            field_dict["next_action"] = next_action
        if operator_summary is not UNSET:
            field_dict["operator_summary"] = operator_summary

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.agent_upgrade_identity_response import AgentUpgradeIdentityResponse # noqa: PLC0415
        from ..models.agent_upgrade_target_diagnostics_response import AgentUpgradeTargetDiagnosticsResponse # noqa: PLC0415
        d = dict(src_dict)
        expected_identity = AgentUpgradeIdentityResponse.from_dict(d.pop("expected_identity"))




        failure_details_unavailable = d.pop("failure_details_unavailable")

        targets = []
        _targets = d.pop("targets")
        for targets_item_data in (_targets):
            targets_item = AgentUpgradeTargetDiagnosticsResponse.from_dict(targets_item_data)



            targets.append(targets_item)


        def _parse_next_action(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        next_action = _parse_next_action(d.pop("next_action", UNSET))


        def _parse_operator_summary(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        operator_summary = _parse_operator_summary(d.pop("operator_summary", UNSET))


        agent_upgrade_diagnostics_response = cls(
            expected_identity=expected_identity,
            failure_details_unavailable=failure_details_unavailable,
            targets=targets,
            next_action=next_action,
            operator_summary=operator_summary,
        )

        return agent_upgrade_diagnostics_response
