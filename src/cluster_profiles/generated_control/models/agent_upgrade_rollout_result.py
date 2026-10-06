from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.agent_upgrade_rollout_result_skipped_type_0 import AgentUpgradeRolloutResultSkippedType0





T = TypeVar("T", bound="AgentUpgradeRolloutResult")



@_attrs_define
class AgentUpgradeRolloutResult:
    """ What a rollout skipped, and the newer rollout that replaced it.

        Attributes:
            skipped (AgentUpgradeRolloutResultSkippedType0 | None | Unset):
            superseded_by (None | str | Unset):
     """

    skipped: AgentUpgradeRolloutResultSkippedType0 | None | Unset = UNSET
    superseded_by: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.agent_upgrade_rollout_result_skipped_type_0 import AgentUpgradeRolloutResultSkippedType0 # noqa: PLC0415
        skipped: dict[str, Any] | None | Unset
        if isinstance(self.skipped, Unset):
            skipped = UNSET
        elif isinstance(self.skipped, AgentUpgradeRolloutResultSkippedType0):
            skipped = self.skipped.to_dict()
        else:
            skipped = self.skipped

        superseded_by: None | str | Unset
        if isinstance(self.superseded_by, Unset):
            superseded_by = UNSET
        else:
            superseded_by = self.superseded_by


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if skipped is not UNSET:
            field_dict["skipped"] = skipped
        if superseded_by is not UNSET:
            field_dict["superseded_by"] = superseded_by

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.agent_upgrade_rollout_result_skipped_type_0 import AgentUpgradeRolloutResultSkippedType0 # noqa: PLC0415
        d = dict(src_dict)
        def _parse_skipped(data: object) -> AgentUpgradeRolloutResultSkippedType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                skipped_type_0 = AgentUpgradeRolloutResultSkippedType0.from_dict(data)



                return skipped_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AgentUpgradeRolloutResultSkippedType0 | None | Unset, data)

        skipped = _parse_skipped(d.pop("skipped", UNSET))


        def _parse_superseded_by(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        superseded_by = _parse_superseded_by(d.pop("superseded_by", UNSET))


        agent_upgrade_rollout_result = cls(
            skipped=skipped,
            superseded_by=superseded_by,
        )

        return agent_upgrade_rollout_result
