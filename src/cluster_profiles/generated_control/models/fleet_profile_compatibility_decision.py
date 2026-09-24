from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_compatibility_decision_kind import check_fleet_profile_compatibility_decision_kind
from ..models.fleet_profile_compatibility_decision_kind import FleetProfileCompatibilityDecisionKind
from ..models.fleet_profile_compatibility_decision_stage import check_fleet_profile_compatibility_decision_stage
from ..models.fleet_profile_compatibility_decision_stage import FleetProfileCompatibilityDecisionStage
from typing import cast
from typing import cast, Union

if TYPE_CHECKING:
  from ..models.compatibility_identity import CompatibilityIdentity





T = TypeVar("T", bound="FleetProfileCompatibilityDecision")



@_attrs_define
class FleetProfileCompatibilityDecision:
    """
        Attributes:
            artifact_sha256 (Union[None, str]):
            compatibility (CompatibilityIdentity): Immutable inputs for an exceptional reusable preparation artifact.
            kind (FleetProfileCompatibilityDecisionKind):
            node_ids (list[str]):
            ready (bool):
            stage (FleetProfileCompatibilityDecisionStage):
     """

    artifact_sha256: Union[None, str]
    compatibility: 'CompatibilityIdentity'
    kind: FleetProfileCompatibilityDecisionKind
    node_ids: list[str]
    ready: bool
    stage: FleetProfileCompatibilityDecisionStage





    def to_dict(self) -> dict[str, Any]:
        from ..models.compatibility_identity import CompatibilityIdentity
        artifact_sha256: Union[None, str]
        artifact_sha256 = self.artifact_sha256

        compatibility = self.compatibility.to_dict()

        kind: str = self.kind

        node_ids = self.node_ids



        ready = self.ready

        stage: str = self.stage


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifact_sha256": artifact_sha256,
            "compatibility": compatibility,
            "kind": kind,
            "node_ids": node_ids,
            "ready": ready,
            "stage": stage,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.compatibility_identity import CompatibilityIdentity
        d = dict(src_dict)
        def _parse_artifact_sha256(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        artifact_sha256 = _parse_artifact_sha256(d.pop("artifact_sha256"))


        compatibility = CompatibilityIdentity.from_dict(d.pop("compatibility"))




        kind = check_fleet_profile_compatibility_decision_kind(d.pop("kind"))




        node_ids = cast(list[str], d.pop("node_ids"))


        ready = d.pop("ready")

        stage = check_fleet_profile_compatibility_decision_stage(d.pop("stage"))




        fleet_profile_compatibility_decision = cls(
            artifact_sha256=artifact_sha256,
            compatibility=compatibility,
            kind=kind,
            node_ids=node_ids,
            ready=ready,
            stage=stage,
        )

        return fleet_profile_compatibility_decision
