from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.artifact_verification_evidence import ArtifactVerificationEvidence
  from ..models.run_switch_child_progress import RunSwitchChildProgress
  from ..models.run_switch_member_receipt import RunSwitchMemberReceipt





T = TypeVar("T", bound="RunSwitchDistributionChildResult")



@_attrs_define
class RunSwitchDistributionChildResult:
    """ Durable projection of one target-copy child operation.

    A child Job has a different persisted shape from a parent phase receipt:
    it owns member progress and per-node handoff evidence.  Keeping that
    projection separate prevents a progress snapshot from being accepted as
    a completed phase result.

        Attributes:
            evidence (list[ArtifactVerificationEvidence]):
            members (list[RunSwitchMemberReceipt]):
            phase (Literal['transfer']):
            progress (RunSwitchChildProgress): Progress nested in a durable child receipt.
            subphase (Literal['target-copy']):
            error_code (None | str | Unset):
            failure_kind (None | str | Unset):
            reason (None | str | Unset):
     """

    evidence: list[ArtifactVerificationEvidence]
    members: list[RunSwitchMemberReceipt]
    phase: Literal['transfer']
    progress: RunSwitchChildProgress
    subphase: Literal['target-copy']
    error_code: None | str | Unset = UNSET
    failure_kind: None | str | Unset = UNSET
    reason: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.artifact_verification_evidence import ArtifactVerificationEvidence # noqa: PLC0415
        from ..models.run_switch_child_progress import RunSwitchChildProgress # noqa: PLC0415
        from ..models.run_switch_member_receipt import RunSwitchMemberReceipt # noqa: PLC0415
        evidence = []
        for evidence_item_data in self.evidence:
            evidence_item = evidence_item_data.to_dict()
            evidence.append(evidence_item)



        members = []
        for members_item_data in self.members:
            members_item = members_item_data.to_dict()
            members.append(members_item)



        phase = self.phase

        progress = self.progress.to_dict()

        subphase = self.subphase

        error_code: None | str | Unset
        if isinstance(self.error_code, Unset):
            error_code = UNSET
        else:
            error_code = self.error_code

        failure_kind: None | str | Unset
        if isinstance(self.failure_kind, Unset):
            failure_kind = UNSET
        else:
            failure_kind = self.failure_kind

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "evidence": evidence,
            "members": members,
            "phase": phase,
            "progress": progress,
            "subphase": subphase,
        })
        if error_code is not UNSET:
            field_dict["error_code"] = error_code
        if failure_kind is not UNSET:
            field_dict["failure_kind"] = failure_kind
        if reason is not UNSET:
            field_dict["reason"] = reason

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.artifact_verification_evidence import ArtifactVerificationEvidence # noqa: PLC0415
        from ..models.run_switch_child_progress import RunSwitchChildProgress # noqa: PLC0415
        from ..models.run_switch_member_receipt import RunSwitchMemberReceipt # noqa: PLC0415
        d = dict(src_dict)
        evidence = []
        _evidence = d.pop("evidence")
        for evidence_item_data in (_evidence):
            evidence_item = ArtifactVerificationEvidence.from_dict(evidence_item_data)



            evidence.append(evidence_item)


        members = []
        _members = d.pop("members")
        for members_item_data in (_members):
            members_item = RunSwitchMemberReceipt.from_dict(members_item_data)



            members.append(members_item)


        phase = cast(Literal['transfer'] , d.pop("phase"))
        if phase != 'transfer':
            raise ValueError(f"phase must match const 'transfer', got '{phase}'")

        progress = RunSwitchChildProgress.from_dict(d.pop("progress"))




        subphase = cast(Literal['target-copy'] , d.pop("subphase"))
        if subphase != 'target-copy':
            raise ValueError(f"subphase must match const 'target-copy', got '{subphase}'")

        def _parse_error_code(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error_code = _parse_error_code(d.pop("error_code", UNSET))


        def _parse_failure_kind(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        failure_kind = _parse_failure_kind(d.pop("failure_kind", UNSET))


        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))


        run_switch_distribution_child_result = cls(
            evidence=evidence,
            members=members,
            phase=phase,
            progress=progress,
            subphase=subphase,
            error_code=error_code,
            failure_kind=failure_kind,
            reason=reason,
        )

        return run_switch_distribution_child_result
