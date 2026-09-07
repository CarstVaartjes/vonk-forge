from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.run_switch_operation_result_completed_phases_item import check_run_switch_operation_result_completed_phases_item
from ..models.run_switch_operation_result_completed_phases_item import RunSwitchOperationResultCompletedPhasesItem
from ..models.run_switch_operation_result_failed_phase_type_0 import check_run_switch_operation_result_failed_phase_type_0
from ..models.run_switch_operation_result_failed_phase_type_0 import RunSwitchOperationResultFailedPhaseType0
from ..models.run_switch_operation_result_phase_type_0 import check_run_switch_operation_result_phase_type_0
from ..models.run_switch_operation_result_phase_type_0 import RunSwitchOperationResultPhaseType0
from ..models.run_switch_operation_result_subphase_type_0 import check_run_switch_operation_result_subphase_type_0
from ..models.run_switch_operation_result_subphase_type_0 import RunSwitchOperationResultSubphaseType0
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.run_switch_runtime_image_result import RunSwitchRuntimeImageResult
  from ..models.run_switch_model_download_result import RunSwitchModelDownloadResult
  from ..models.run_switch_member_receipt import RunSwitchMemberReceipt
  from ..models.run_switch_target_transfer_result import RunSwitchTargetTransferResult
  from ..models.run_switch_prepared_result import RunSwitchPreparedResult
  from ..models.run_switch_cached_transfer_result import RunSwitchCachedTransferResult
  from ..models.run_switch_model_download_pending_result import RunSwitchModelDownloadPendingResult
  from ..models.run_switch_container_build_result import RunSwitchContainerBuildResult
  from ..models.run_switch_cleanup_result import RunSwitchCleanupResult
  from ..models.run_switch_final_verify_result import RunSwitchFinalVerifyResult
  from ..models.run_switch_start_result import RunSwitchStartResult
  from ..models.run_switch_runtime_install_result import RunSwitchRuntimeInstallResult
  from ..models.run_switch_target_transfer_evidence_result import RunSwitchTargetTransferEvidenceResult
  from ..models.run_switch_stop_result import RunSwitchStopResult
  from ..models.run_switch_verify_result import RunSwitchVerifyResult
  from ..models.run_switch_runtime_plan_result import RunSwitchRuntimePlanResult





T = TypeVar("T", bound="RunSwitchOperationResult")



@_attrs_define
class RunSwitchOperationResult:
    """ Exact durable result tree stored in ``Job.result``.

        Attributes:
            child_operation_id (Union[None, Unset, str]):
            completed_bytes (Union[Unset, int]):  Default: 0.
            completed_phases (Union[Unset, list[RunSwitchOperationResultCompletedPhasesItem]]):
            failed_phase (Union[None, RunSwitchOperationResultFailedPhaseType0, Unset]):
            final_observation (Union['RunSwitchCachedTransferResult', 'RunSwitchCleanupResult',
                'RunSwitchContainerBuildResult', 'RunSwitchFinalVerifyResult', 'RunSwitchModelDownloadPendingResult',
                'RunSwitchModelDownloadResult', 'RunSwitchPreparedResult', 'RunSwitchRuntimeImageResult',
                'RunSwitchRuntimeInstallResult', 'RunSwitchRuntimePlanResult', 'RunSwitchStartResult', 'RunSwitchStopResult',
                'RunSwitchTargetTransferEvidenceResult', 'RunSwitchTargetTransferResult', 'RunSwitchVerifyResult', None,
                Unset]):
            final_verify_started_at (Union[None, Unset, float]):
            item_index (Union[Unset, int]):  Default: 0.
            members (Union[Unset, list['RunSwitchMemberReceipt']]):
            phase (Union[None, RunSwitchOperationResultPhaseType0, Unset]):
            phase_index (Union[Unset, int]):  Default: 0.
            phase_results (Union[Unset, list[Union['RunSwitchCachedTransferResult', 'RunSwitchCleanupResult',
                'RunSwitchContainerBuildResult', 'RunSwitchFinalVerifyResult', 'RunSwitchModelDownloadPendingResult',
                'RunSwitchModelDownloadResult', 'RunSwitchPreparedResult', 'RunSwitchRuntimeImageResult',
                'RunSwitchRuntimeInstallResult', 'RunSwitchRuntimePlanResult', 'RunSwitchStartResult', 'RunSwitchStopResult',
                'RunSwitchTargetTransferEvidenceResult', 'RunSwitchTargetTransferResult', 'RunSwitchVerifyResult']]]):
            retry_attempt (Union[None, Unset, int]):
            retry_reason (Union[None, Unset, str]):
            retryable (Union[Unset, bool]):  Default: False.
            subphase (Union[None, RunSwitchOperationResultSubphaseType0, Unset]):
            total_bytes (Union[None, Unset, int]):
            total_bytes_known (Union[Unset, bool]):  Default: False.
     """

    child_operation_id: Union[None, Unset, str] = UNSET
    completed_bytes: Union[Unset, int] = 0
    completed_phases: Union[Unset, list[RunSwitchOperationResultCompletedPhasesItem]] = UNSET
    failed_phase: Union[None, RunSwitchOperationResultFailedPhaseType0, Unset] = UNSET
    final_observation: Union['RunSwitchCachedTransferResult', 'RunSwitchCleanupResult', 'RunSwitchContainerBuildResult', 'RunSwitchFinalVerifyResult', 'RunSwitchModelDownloadPendingResult', 'RunSwitchModelDownloadResult', 'RunSwitchPreparedResult', 'RunSwitchRuntimeImageResult', 'RunSwitchRuntimeInstallResult', 'RunSwitchRuntimePlanResult', 'RunSwitchStartResult', 'RunSwitchStopResult', 'RunSwitchTargetTransferEvidenceResult', 'RunSwitchTargetTransferResult', 'RunSwitchVerifyResult', None, Unset] = UNSET
    final_verify_started_at: Union[None, Unset, float] = UNSET
    item_index: Union[Unset, int] = 0
    members: Union[Unset, list['RunSwitchMemberReceipt']] = UNSET
    phase: Union[None, RunSwitchOperationResultPhaseType0, Unset] = UNSET
    phase_index: Union[Unset, int] = 0
    phase_results: Union[Unset, list[Union['RunSwitchCachedTransferResult', 'RunSwitchCleanupResult', 'RunSwitchContainerBuildResult', 'RunSwitchFinalVerifyResult', 'RunSwitchModelDownloadPendingResult', 'RunSwitchModelDownloadResult', 'RunSwitchPreparedResult', 'RunSwitchRuntimeImageResult', 'RunSwitchRuntimeInstallResult', 'RunSwitchRuntimePlanResult', 'RunSwitchStartResult', 'RunSwitchStopResult', 'RunSwitchTargetTransferEvidenceResult', 'RunSwitchTargetTransferResult', 'RunSwitchVerifyResult']]] = UNSET
    retry_attempt: Union[None, Unset, int] = UNSET
    retry_reason: Union[None, Unset, str] = UNSET
    retryable: Union[Unset, bool] = False
    subphase: Union[None, RunSwitchOperationResultSubphaseType0, Unset] = UNSET
    total_bytes: Union[None, Unset, int] = UNSET
    total_bytes_known: Union[Unset, bool] = False





    def to_dict(self) -> dict[str, Any]:
        from ..models.run_switch_runtime_image_result import RunSwitchRuntimeImageResult
        from ..models.run_switch_model_download_result import RunSwitchModelDownloadResult
        from ..models.run_switch_member_receipt import RunSwitchMemberReceipt
        from ..models.run_switch_target_transfer_result import RunSwitchTargetTransferResult
        from ..models.run_switch_prepared_result import RunSwitchPreparedResult
        from ..models.run_switch_cached_transfer_result import RunSwitchCachedTransferResult
        from ..models.run_switch_model_download_pending_result import RunSwitchModelDownloadPendingResult
        from ..models.run_switch_container_build_result import RunSwitchContainerBuildResult
        from ..models.run_switch_cleanup_result import RunSwitchCleanupResult
        from ..models.run_switch_final_verify_result import RunSwitchFinalVerifyResult
        from ..models.run_switch_start_result import RunSwitchStartResult
        from ..models.run_switch_runtime_install_result import RunSwitchRuntimeInstallResult
        from ..models.run_switch_target_transfer_evidence_result import RunSwitchTargetTransferEvidenceResult
        from ..models.run_switch_stop_result import RunSwitchStopResult
        from ..models.run_switch_verify_result import RunSwitchVerifyResult
        from ..models.run_switch_runtime_plan_result import RunSwitchRuntimePlanResult
        child_operation_id: Union[None, Unset, str]
        if isinstance(self.child_operation_id, Unset):
            child_operation_id = UNSET
        else:
            child_operation_id = self.child_operation_id

        completed_bytes = self.completed_bytes

        completed_phases: Union[Unset, list[str]] = UNSET
        if not isinstance(self.completed_phases, Unset):
            completed_phases = []
            for completed_phases_item_data in self.completed_phases:
                completed_phases_item: str = completed_phases_item_data
                completed_phases.append(completed_phases_item)



        failed_phase: Union[None, Unset, str]
        if isinstance(self.failed_phase, Unset):
            failed_phase = UNSET
        elif isinstance(self.failed_phase, str):
            failed_phase = self.failed_phase
        else:
            failed_phase = self.failed_phase

        final_observation: Union[None, Unset, dict[str, Any]]
        if isinstance(self.final_observation, Unset):
            final_observation = UNSET
        elif isinstance(self.final_observation, RunSwitchContainerBuildResult):
            final_observation = self.final_observation.to_dict()
        elif isinstance(self.final_observation, RunSwitchRuntimeImageResult):
            final_observation = self.final_observation.to_dict()
        elif isinstance(self.final_observation, RunSwitchModelDownloadResult):
            final_observation = self.final_observation.to_dict()
        elif isinstance(self.final_observation, RunSwitchModelDownloadPendingResult):
            final_observation = self.final_observation.to_dict()
        elif isinstance(self.final_observation, RunSwitchTargetTransferResult):
            final_observation = self.final_observation.to_dict()
        elif isinstance(self.final_observation, RunSwitchCachedTransferResult):
            final_observation = self.final_observation.to_dict()
        elif isinstance(self.final_observation, RunSwitchTargetTransferEvidenceResult):
            final_observation = self.final_observation.to_dict()
        elif isinstance(self.final_observation, RunSwitchVerifyResult):
            final_observation = self.final_observation.to_dict()
        elif isinstance(self.final_observation, RunSwitchCleanupResult):
            final_observation = self.final_observation.to_dict()
        elif isinstance(self.final_observation, RunSwitchRuntimePlanResult):
            final_observation = self.final_observation.to_dict()
        elif isinstance(self.final_observation, RunSwitchPreparedResult):
            final_observation = self.final_observation.to_dict()
        elif isinstance(self.final_observation, RunSwitchRuntimeInstallResult):
            final_observation = self.final_observation.to_dict()
        elif isinstance(self.final_observation, RunSwitchStopResult):
            final_observation = self.final_observation.to_dict()
        elif isinstance(self.final_observation, RunSwitchStartResult):
            final_observation = self.final_observation.to_dict()
        elif isinstance(self.final_observation, RunSwitchFinalVerifyResult):
            final_observation = self.final_observation.to_dict()
        else:
            final_observation = self.final_observation

        final_verify_started_at: Union[None, Unset, float]
        if isinstance(self.final_verify_started_at, Unset):
            final_verify_started_at = UNSET
        else:
            final_verify_started_at = self.final_verify_started_at

        item_index = self.item_index

        members: Union[Unset, list[dict[str, Any]]] = UNSET
        if not isinstance(self.members, Unset):
            members = []
            for members_item_data in self.members:
                members_item = members_item_data.to_dict()
                members.append(members_item)



        phase: Union[None, Unset, str]
        if isinstance(self.phase, Unset):
            phase = UNSET
        elif isinstance(self.phase, str):
            phase = self.phase
        else:
            phase = self.phase

        phase_index = self.phase_index

        phase_results: Union[Unset, list[dict[str, Any]]] = UNSET
        if not isinstance(self.phase_results, Unset):
            phase_results = []
            for phase_results_item_data in self.phase_results:
                phase_results_item: dict[str, Any]
                if isinstance(phase_results_item_data, RunSwitchContainerBuildResult):
                    phase_results_item = phase_results_item_data.to_dict()
                elif isinstance(phase_results_item_data, RunSwitchRuntimeImageResult):
                    phase_results_item = phase_results_item_data.to_dict()
                elif isinstance(phase_results_item_data, RunSwitchModelDownloadResult):
                    phase_results_item = phase_results_item_data.to_dict()
                elif isinstance(phase_results_item_data, RunSwitchModelDownloadPendingResult):
                    phase_results_item = phase_results_item_data.to_dict()
                elif isinstance(phase_results_item_data, RunSwitchTargetTransferResult):
                    phase_results_item = phase_results_item_data.to_dict()
                elif isinstance(phase_results_item_data, RunSwitchCachedTransferResult):
                    phase_results_item = phase_results_item_data.to_dict()
                elif isinstance(phase_results_item_data, RunSwitchTargetTransferEvidenceResult):
                    phase_results_item = phase_results_item_data.to_dict()
                elif isinstance(phase_results_item_data, RunSwitchVerifyResult):
                    phase_results_item = phase_results_item_data.to_dict()
                elif isinstance(phase_results_item_data, RunSwitchCleanupResult):
                    phase_results_item = phase_results_item_data.to_dict()
                elif isinstance(phase_results_item_data, RunSwitchRuntimePlanResult):
                    phase_results_item = phase_results_item_data.to_dict()
                elif isinstance(phase_results_item_data, RunSwitchPreparedResult):
                    phase_results_item = phase_results_item_data.to_dict()
                elif isinstance(phase_results_item_data, RunSwitchRuntimeInstallResult):
                    phase_results_item = phase_results_item_data.to_dict()
                elif isinstance(phase_results_item_data, RunSwitchStopResult):
                    phase_results_item = phase_results_item_data.to_dict()
                elif isinstance(phase_results_item_data, RunSwitchStartResult):
                    phase_results_item = phase_results_item_data.to_dict()
                else:
                    phase_results_item = phase_results_item_data.to_dict()

                phase_results.append(phase_results_item)



        retry_attempt: Union[None, Unset, int]
        if isinstance(self.retry_attempt, Unset):
            retry_attempt = UNSET
        else:
            retry_attempt = self.retry_attempt

        retry_reason: Union[None, Unset, str]
        if isinstance(self.retry_reason, Unset):
            retry_reason = UNSET
        else:
            retry_reason = self.retry_reason

        retryable = self.retryable

        subphase: Union[None, Unset, str]
        if isinstance(self.subphase, Unset):
            subphase = UNSET
        elif isinstance(self.subphase, str):
            subphase = self.subphase
        else:
            subphase = self.subphase

        total_bytes: Union[None, Unset, int]
        if isinstance(self.total_bytes, Unset):
            total_bytes = UNSET
        else:
            total_bytes = self.total_bytes

        total_bytes_known = self.total_bytes_known


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if child_operation_id is not UNSET:
            field_dict["child_operation_id"] = child_operation_id
        if completed_bytes is not UNSET:
            field_dict["completed_bytes"] = completed_bytes
        if completed_phases is not UNSET:
            field_dict["completed_phases"] = completed_phases
        if failed_phase is not UNSET:
            field_dict["failed_phase"] = failed_phase
        if final_observation is not UNSET:
            field_dict["final_observation"] = final_observation
        if final_verify_started_at is not UNSET:
            field_dict["final_verify_started_at"] = final_verify_started_at
        if item_index is not UNSET:
            field_dict["item_index"] = item_index
        if members is not UNSET:
            field_dict["members"] = members
        if phase is not UNSET:
            field_dict["phase"] = phase
        if phase_index is not UNSET:
            field_dict["phase_index"] = phase_index
        if phase_results is not UNSET:
            field_dict["phase_results"] = phase_results
        if retry_attempt is not UNSET:
            field_dict["retry_attempt"] = retry_attempt
        if retry_reason is not UNSET:
            field_dict["retry_reason"] = retry_reason
        if retryable is not UNSET:
            field_dict["retryable"] = retryable
        if subphase is not UNSET:
            field_dict["subphase"] = subphase
        if total_bytes is not UNSET:
            field_dict["total_bytes"] = total_bytes
        if total_bytes_known is not UNSET:
            field_dict["total_bytes_known"] = total_bytes_known

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.run_switch_runtime_image_result import RunSwitchRuntimeImageResult
        from ..models.run_switch_model_download_result import RunSwitchModelDownloadResult
        from ..models.run_switch_member_receipt import RunSwitchMemberReceipt
        from ..models.run_switch_target_transfer_result import RunSwitchTargetTransferResult
        from ..models.run_switch_prepared_result import RunSwitchPreparedResult
        from ..models.run_switch_cached_transfer_result import RunSwitchCachedTransferResult
        from ..models.run_switch_model_download_pending_result import RunSwitchModelDownloadPendingResult
        from ..models.run_switch_container_build_result import RunSwitchContainerBuildResult
        from ..models.run_switch_cleanup_result import RunSwitchCleanupResult
        from ..models.run_switch_final_verify_result import RunSwitchFinalVerifyResult
        from ..models.run_switch_start_result import RunSwitchStartResult
        from ..models.run_switch_runtime_install_result import RunSwitchRuntimeInstallResult
        from ..models.run_switch_target_transfer_evidence_result import RunSwitchTargetTransferEvidenceResult
        from ..models.run_switch_stop_result import RunSwitchStopResult
        from ..models.run_switch_verify_result import RunSwitchVerifyResult
        from ..models.run_switch_runtime_plan_result import RunSwitchRuntimePlanResult
        d = dict(src_dict)
        def _parse_child_operation_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        child_operation_id = _parse_child_operation_id(d.pop("child_operation_id", UNSET))


        completed_bytes = d.pop("completed_bytes", UNSET)

        completed_phases = []
        _completed_phases = d.pop("completed_phases", UNSET)
        for completed_phases_item_data in (_completed_phases or []):
            completed_phases_item = check_run_switch_operation_result_completed_phases_item(completed_phases_item_data)



            completed_phases.append(completed_phases_item)


        def _parse_failed_phase(data: object) -> Union[None, RunSwitchOperationResultFailedPhaseType0, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                failed_phase_type_0 = check_run_switch_operation_result_failed_phase_type_0(data)



                return failed_phase_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, RunSwitchOperationResultFailedPhaseType0, Unset], data)

        failed_phase = _parse_failed_phase(d.pop("failed_phase", UNSET))


        def _parse_final_observation(data: object) -> Union['RunSwitchCachedTransferResult', 'RunSwitchCleanupResult', 'RunSwitchContainerBuildResult', 'RunSwitchFinalVerifyResult', 'RunSwitchModelDownloadPendingResult', 'RunSwitchModelDownloadResult', 'RunSwitchPreparedResult', 'RunSwitchRuntimeImageResult', 'RunSwitchRuntimeInstallResult', 'RunSwitchRuntimePlanResult', 'RunSwitchStartResult', 'RunSwitchStopResult', 'RunSwitchTargetTransferEvidenceResult', 'RunSwitchTargetTransferResult', 'RunSwitchVerifyResult', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                final_observation_type_0 = RunSwitchContainerBuildResult.from_dict(data)



                return final_observation_type_0
            except: # noqa: E722
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                final_observation_type_1 = RunSwitchRuntimeImageResult.from_dict(data)



                return final_observation_type_1
            except: # noqa: E722
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                final_observation_type_2 = RunSwitchModelDownloadResult.from_dict(data)



                return final_observation_type_2
            except: # noqa: E722
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                final_observation_type_3 = RunSwitchModelDownloadPendingResult.from_dict(data)



                return final_observation_type_3
            except: # noqa: E722
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                final_observation_type_4 = RunSwitchTargetTransferResult.from_dict(data)



                return final_observation_type_4
            except: # noqa: E722
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                final_observation_type_5 = RunSwitchCachedTransferResult.from_dict(data)



                return final_observation_type_5
            except: # noqa: E722
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                final_observation_type_6 = RunSwitchTargetTransferEvidenceResult.from_dict(data)



                return final_observation_type_6
            except: # noqa: E722
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                final_observation_type_7 = RunSwitchVerifyResult.from_dict(data)



                return final_observation_type_7
            except: # noqa: E722
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                final_observation_type_8 = RunSwitchCleanupResult.from_dict(data)



                return final_observation_type_8
            except: # noqa: E722
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                final_observation_type_9 = RunSwitchRuntimePlanResult.from_dict(data)



                return final_observation_type_9
            except: # noqa: E722
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                final_observation_type_10 = RunSwitchPreparedResult.from_dict(data)



                return final_observation_type_10
            except: # noqa: E722
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                final_observation_type_11 = RunSwitchRuntimeInstallResult.from_dict(data)



                return final_observation_type_11
            except: # noqa: E722
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                final_observation_type_12 = RunSwitchStopResult.from_dict(data)



                return final_observation_type_12
            except: # noqa: E722
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                final_observation_type_13 = RunSwitchStartResult.from_dict(data)



                return final_observation_type_13
            except: # noqa: E722
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                final_observation_type_14 = RunSwitchFinalVerifyResult.from_dict(data)



                return final_observation_type_14
            except: # noqa: E722
                pass
            return cast(Union['RunSwitchCachedTransferResult', 'RunSwitchCleanupResult', 'RunSwitchContainerBuildResult', 'RunSwitchFinalVerifyResult', 'RunSwitchModelDownloadPendingResult', 'RunSwitchModelDownloadResult', 'RunSwitchPreparedResult', 'RunSwitchRuntimeImageResult', 'RunSwitchRuntimeInstallResult', 'RunSwitchRuntimePlanResult', 'RunSwitchStartResult', 'RunSwitchStopResult', 'RunSwitchTargetTransferEvidenceResult', 'RunSwitchTargetTransferResult', 'RunSwitchVerifyResult', None, Unset], data)

        final_observation = _parse_final_observation(d.pop("final_observation", UNSET))


        def _parse_final_verify_started_at(data: object) -> Union[None, Unset, float]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, float], data)

        final_verify_started_at = _parse_final_verify_started_at(d.pop("final_verify_started_at", UNSET))


        item_index = d.pop("item_index", UNSET)

        members = []
        _members = d.pop("members", UNSET)
        for members_item_data in (_members or []):
            members_item = RunSwitchMemberReceipt.from_dict(members_item_data)



            members.append(members_item)


        def _parse_phase(data: object) -> Union[None, RunSwitchOperationResultPhaseType0, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                phase_type_0 = check_run_switch_operation_result_phase_type_0(data)



                return phase_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, RunSwitchOperationResultPhaseType0, Unset], data)

        phase = _parse_phase(d.pop("phase", UNSET))


        phase_index = d.pop("phase_index", UNSET)

        phase_results = []
        _phase_results = d.pop("phase_results", UNSET)
        for phase_results_item_data in (_phase_results or []):
            def _parse_phase_results_item(data: object) -> Union['RunSwitchCachedTransferResult', 'RunSwitchCleanupResult', 'RunSwitchContainerBuildResult', 'RunSwitchFinalVerifyResult', 'RunSwitchModelDownloadPendingResult', 'RunSwitchModelDownloadResult', 'RunSwitchPreparedResult', 'RunSwitchRuntimeImageResult', 'RunSwitchRuntimeInstallResult', 'RunSwitchRuntimePlanResult', 'RunSwitchStartResult', 'RunSwitchStopResult', 'RunSwitchTargetTransferEvidenceResult', 'RunSwitchTargetTransferResult', 'RunSwitchVerifyResult']:
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    phase_results_item_type_0 = RunSwitchContainerBuildResult.from_dict(data)



                    return phase_results_item_type_0
                except: # noqa: E722
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    phase_results_item_type_1 = RunSwitchRuntimeImageResult.from_dict(data)



                    return phase_results_item_type_1
                except: # noqa: E722
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    phase_results_item_type_2 = RunSwitchModelDownloadResult.from_dict(data)



                    return phase_results_item_type_2
                except: # noqa: E722
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    phase_results_item_type_3 = RunSwitchModelDownloadPendingResult.from_dict(data)



                    return phase_results_item_type_3
                except: # noqa: E722
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    phase_results_item_type_4 = RunSwitchTargetTransferResult.from_dict(data)



                    return phase_results_item_type_4
                except: # noqa: E722
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    phase_results_item_type_5 = RunSwitchCachedTransferResult.from_dict(data)



                    return phase_results_item_type_5
                except: # noqa: E722
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    phase_results_item_type_6 = RunSwitchTargetTransferEvidenceResult.from_dict(data)



                    return phase_results_item_type_6
                except: # noqa: E722
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    phase_results_item_type_7 = RunSwitchVerifyResult.from_dict(data)



                    return phase_results_item_type_7
                except: # noqa: E722
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    phase_results_item_type_8 = RunSwitchCleanupResult.from_dict(data)



                    return phase_results_item_type_8
                except: # noqa: E722
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    phase_results_item_type_9 = RunSwitchRuntimePlanResult.from_dict(data)



                    return phase_results_item_type_9
                except: # noqa: E722
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    phase_results_item_type_10 = RunSwitchPreparedResult.from_dict(data)



                    return phase_results_item_type_10
                except: # noqa: E722
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    phase_results_item_type_11 = RunSwitchRuntimeInstallResult.from_dict(data)



                    return phase_results_item_type_11
                except: # noqa: E722
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    phase_results_item_type_12 = RunSwitchStopResult.from_dict(data)



                    return phase_results_item_type_12
                except: # noqa: E722
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    phase_results_item_type_13 = RunSwitchStartResult.from_dict(data)



                    return phase_results_item_type_13
                except: # noqa: E722
                    pass
                if not isinstance(data, dict):
                    raise TypeError()
                phase_results_item_type_14 = RunSwitchFinalVerifyResult.from_dict(data)



                return phase_results_item_type_14

            phase_results_item = _parse_phase_results_item(phase_results_item_data)

            phase_results.append(phase_results_item)


        def _parse_retry_attempt(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        retry_attempt = _parse_retry_attempt(d.pop("retry_attempt", UNSET))


        def _parse_retry_reason(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        retry_reason = _parse_retry_reason(d.pop("retry_reason", UNSET))


        retryable = d.pop("retryable", UNSET)

        def _parse_subphase(data: object) -> Union[None, RunSwitchOperationResultSubphaseType0, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                subphase_type_0 = check_run_switch_operation_result_subphase_type_0(data)



                return subphase_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, RunSwitchOperationResultSubphaseType0, Unset], data)

        subphase = _parse_subphase(d.pop("subphase", UNSET))


        def _parse_total_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        total_bytes = _parse_total_bytes(d.pop("total_bytes", UNSET))


        total_bytes_known = d.pop("total_bytes_known", UNSET)

        run_switch_operation_result = cls(
            child_operation_id=child_operation_id,
            completed_bytes=completed_bytes,
            completed_phases=completed_phases,
            failed_phase=failed_phase,
            final_observation=final_observation,
            final_verify_started_at=final_verify_started_at,
            item_index=item_index,
            members=members,
            phase=phase,
            phase_index=phase_index,
            phase_results=phase_results,
            retry_attempt=retry_attempt,
            retry_reason=retry_reason,
            retryable=retryable,
            subphase=subphase,
            total_bytes=total_bytes,
            total_bytes_known=total_bytes_known,
        )

        return run_switch_operation_result
