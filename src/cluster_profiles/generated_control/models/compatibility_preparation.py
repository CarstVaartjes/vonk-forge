from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.compatibility_preparation_kind import check_compatibility_preparation_kind
from ..models.compatibility_preparation_kind import CompatibilityPreparationKind
from ..models.compatibility_preparation_stage import check_compatibility_preparation_stage
from ..models.compatibility_preparation_stage import CompatibilityPreparationStage
from ..models.compatibility_preparation_state import check_compatibility_preparation_state
from ..models.compatibility_preparation_state import CompatibilityPreparationState
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.compatibility_identity import CompatibilityIdentity





T = TypeVar("T", bound="CompatibilityPreparation")



@_attrs_define
class CompatibilityPreparation:
    """ Explicit reusable work that cannot be embedded in the base image.

        Attributes:
            compatibility (CompatibilityIdentity): Immutable inputs for an exceptional reusable preparation artifact.
            compatibility_key_sha256 (str):
            kind (CompatibilityPreparationKind):
            reusable (bool):
            stage (CompatibilityPreparationStage):
            state (CompatibilityPreparationState):
            artifact_sha256 (Union[None, Unset, str]):
            node_ids (Union[Unset, list[str]]):
            reason (Union[None, Unset, str]):
     """

    compatibility: 'CompatibilityIdentity'
    compatibility_key_sha256: str
    kind: CompatibilityPreparationKind
    reusable: bool
    stage: CompatibilityPreparationStage
    state: CompatibilityPreparationState
    artifact_sha256: Union[None, Unset, str] = UNSET
    node_ids: Union[Unset, list[str]] = UNSET
    reason: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.compatibility_identity import CompatibilityIdentity
        compatibility = self.compatibility.to_dict()

        compatibility_key_sha256 = self.compatibility_key_sha256

        kind: str = self.kind

        reusable = self.reusable

        stage: str = self.stage

        state: str = self.state

        artifact_sha256: Union[None, Unset, str]
        if isinstance(self.artifact_sha256, Unset):
            artifact_sha256 = UNSET
        else:
            artifact_sha256 = self.artifact_sha256

        node_ids: Union[Unset, list[str]] = UNSET
        if not isinstance(self.node_ids, Unset):
            node_ids = self.node_ids



        reason: Union[None, Unset, str]
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "compatibility": compatibility,
            "compatibility_key_sha256": compatibility_key_sha256,
            "kind": kind,
            "reusable": reusable,
            "stage": stage,
            "state": state,
        })
        if artifact_sha256 is not UNSET:
            field_dict["artifact_sha256"] = artifact_sha256
        if node_ids is not UNSET:
            field_dict["node_ids"] = node_ids
        if reason is not UNSET:
            field_dict["reason"] = reason

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.compatibility_identity import CompatibilityIdentity
        d = dict(src_dict)
        compatibility = CompatibilityIdentity.from_dict(d.pop("compatibility"))




        compatibility_key_sha256 = d.pop("compatibility_key_sha256")

        kind = check_compatibility_preparation_kind(d.pop("kind"))




        reusable = d.pop("reusable")

        stage = check_compatibility_preparation_stage(d.pop("stage"))




        state = check_compatibility_preparation_state(d.pop("state"))




        def _parse_artifact_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        artifact_sha256 = _parse_artifact_sha256(d.pop("artifact_sha256", UNSET))


        node_ids = cast(list[str], d.pop("node_ids", UNSET))


        def _parse_reason(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        reason = _parse_reason(d.pop("reason", UNSET))


        compatibility_preparation = cls(
            compatibility=compatibility,
            compatibility_key_sha256=compatibility_key_sha256,
            kind=kind,
            reusable=reusable,
            stage=stage,
            state=state,
            artifact_sha256=artifact_sha256,
            node_ids=node_ids,
            reason=reason,
        )

        return compatibility_preparation
