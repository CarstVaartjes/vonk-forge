from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.distribution_job_payload_phase import check_distribution_job_payload_phase
from ..models.distribution_job_payload_phase import DistributionJobPayloadPhase
from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.distribution_job_payload_assignments import DistributionJobPayloadAssignments
  from ..models.distribution_job_payload_target_totals import DistributionJobPayloadTargetTotals
  from ..models.distribution_transfer_progress import DistributionTransferProgress





T = TypeVar("T", bound="DistributionJobPayload")



@_attrs_define
class DistributionJobPayload:
    """ One target-copy child: the plan it serves and what each Spark must receive.

        Attributes:
            assignments (DistributionJobPayloadAssignments):
            cached_nodes (list[str]):
            phase (DistributionJobPayloadPhase):
            plan_digest (str):
            progress (DistributionTransferProgress):
            target_order (list[str]):
            target_totals (DistributionJobPayloadTargetTotals):
            workload_intent_ordinal (int | None | Unset):
     """

    assignments: DistributionJobPayloadAssignments
    cached_nodes: list[str]
    phase: DistributionJobPayloadPhase
    plan_digest: str
    progress: DistributionTransferProgress
    target_order: list[str]
    target_totals: DistributionJobPayloadTargetTotals
    workload_intent_ordinal: int | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.distribution_job_payload_assignments import DistributionJobPayloadAssignments # noqa: PLC0415
        from ..models.distribution_job_payload_target_totals import DistributionJobPayloadTargetTotals # noqa: PLC0415
        from ..models.distribution_transfer_progress import DistributionTransferProgress # noqa: PLC0415
        assignments = self.assignments.to_dict()

        cached_nodes = self.cached_nodes



        phase: str = self.phase

        plan_digest = self.plan_digest

        progress = self.progress.to_dict()

        target_order = self.target_order



        target_totals = self.target_totals.to_dict()

        workload_intent_ordinal: int | None | Unset
        if isinstance(self.workload_intent_ordinal, Unset):
            workload_intent_ordinal = UNSET
        else:
            workload_intent_ordinal = self.workload_intent_ordinal


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "assignments": assignments,
            "cached_nodes": cached_nodes,
            "phase": phase,
            "plan_digest": plan_digest,
            "progress": progress,
            "target_order": target_order,
            "target_totals": target_totals,
        })
        if workload_intent_ordinal is not UNSET:
            field_dict["workload_intent_ordinal"] = workload_intent_ordinal

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.distribution_job_payload_assignments import DistributionJobPayloadAssignments # noqa: PLC0415
        from ..models.distribution_job_payload_target_totals import DistributionJobPayloadTargetTotals # noqa: PLC0415
        from ..models.distribution_transfer_progress import DistributionTransferProgress # noqa: PLC0415
        d = dict(src_dict)
        assignments = DistributionJobPayloadAssignments.from_dict(d.pop("assignments"))




        cached_nodes = cast(list[str], d.pop("cached_nodes"))


        phase = check_distribution_job_payload_phase(d.pop("phase"))




        plan_digest = d.pop("plan_digest")

        progress = DistributionTransferProgress.from_dict(d.pop("progress"))




        target_order = cast(list[str], d.pop("target_order"))


        target_totals = DistributionJobPayloadTargetTotals.from_dict(d.pop("target_totals"))




        def _parse_workload_intent_ordinal(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        workload_intent_ordinal = _parse_workload_intent_ordinal(d.pop("workload_intent_ordinal", UNSET))


        distribution_job_payload = cls(
            assignments=assignments,
            cached_nodes=cached_nodes,
            phase=phase,
            plan_digest=plan_digest,
            progress=progress,
            target_order=target_order,
            target_totals=target_totals,
            workload_intent_ordinal=workload_intent_ordinal,
        )

        return distribution_job_payload
