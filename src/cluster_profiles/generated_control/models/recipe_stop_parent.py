from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_stop_parent_owner_kind import check_recipe_stop_parent_owner_kind
from ..models.recipe_stop_parent_owner_kind import RecipeStopParentOwnerKind
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.distributed_recovery_marker import DistributedRecoveryMarker
  from ..models.job_run_stop_scope import JobRunStopScope
  from ..models.profile_partial_stop import ProfilePartialStop
  from ..models.service_run_stop_review import ServiceRunStopReview
  from ..models.stop_phase_operation import StopPhaseOperation





T = TypeVar("T", bound="RecipeStopParent")



@_attrs_define
class RecipeStopParent:
    """
        Attributes:
            owner_id (str):
            owner_kind (RecipeStopParentOwnerKind):
            plan_digest (str):
            schema_version (Literal[1]):
            execution_mode (Literal['one-shot-jobs'] | None | Unset):
            job_run_stop_authorization (JobRunStopScope | None | Unset):
            phases (list[list[StopPhaseOperation]] | None | Unset):
            profile_partial_stop (None | ProfilePartialStop | Unset):
            recovery (DistributedRecoveryMarker | None | Unset):
            service_stop_review (None | ServiceRunStopReview | Unset):
            workload_intent_ordinal (int | None | Unset):
     """

    owner_id: str
    owner_kind: RecipeStopParentOwnerKind
    plan_digest: str
    schema_version: Literal[1]
    execution_mode: Literal['one-shot-jobs'] | None | Unset = UNSET
    job_run_stop_authorization: JobRunStopScope | None | Unset = UNSET
    phases: list[list[StopPhaseOperation]] | None | Unset = UNSET
    profile_partial_stop: None | ProfilePartialStop | Unset = UNSET
    recovery: DistributedRecoveryMarker | None | Unset = UNSET
    service_stop_review: None | ServiceRunStopReview | Unset = UNSET
    workload_intent_ordinal: int | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.distributed_recovery_marker import DistributedRecoveryMarker # noqa: PLC0415
        from ..models.job_run_stop_scope import JobRunStopScope # noqa: PLC0415
        from ..models.profile_partial_stop import ProfilePartialStop # noqa: PLC0415
        from ..models.service_run_stop_review import ServiceRunStopReview # noqa: PLC0415
        from ..models.stop_phase_operation import StopPhaseOperation # noqa: PLC0415
        owner_id = self.owner_id

        owner_kind: str = self.owner_kind

        plan_digest = self.plan_digest

        schema_version = self.schema_version

        execution_mode: Literal['one-shot-jobs'] | None | Unset
        if isinstance(self.execution_mode, Unset):
            execution_mode = UNSET
        else:
            execution_mode = self.execution_mode

        job_run_stop_authorization: dict[str, Any] | None | Unset
        if isinstance(self.job_run_stop_authorization, Unset):
            job_run_stop_authorization = UNSET
        elif isinstance(self.job_run_stop_authorization, JobRunStopScope):
            job_run_stop_authorization = self.job_run_stop_authorization.to_dict()
        else:
            job_run_stop_authorization = self.job_run_stop_authorization

        phases: list[list[dict[str, Any]]] | None | Unset
        if isinstance(self.phases, Unset):
            phases = UNSET
        elif isinstance(self.phases, list):
            phases = []
            for phases_type_0_item_data in self.phases:
                phases_type_0_item = []
                for phases_type_0_item_item_data in phases_type_0_item_data:
                    phases_type_0_item_item = phases_type_0_item_item_data.to_dict()
                    phases_type_0_item.append(phases_type_0_item_item)


                phases.append(phases_type_0_item)


        else:
            phases = self.phases

        profile_partial_stop: dict[str, Any] | None | Unset
        if isinstance(self.profile_partial_stop, Unset):
            profile_partial_stop = UNSET
        elif isinstance(self.profile_partial_stop, ProfilePartialStop):
            profile_partial_stop = self.profile_partial_stop.to_dict()
        else:
            profile_partial_stop = self.profile_partial_stop

        recovery: dict[str, Any] | None | Unset
        if isinstance(self.recovery, Unset):
            recovery = UNSET
        elif isinstance(self.recovery, DistributedRecoveryMarker):
            recovery = self.recovery.to_dict()
        else:
            recovery = self.recovery

        service_stop_review: dict[str, Any] | None | Unset
        if isinstance(self.service_stop_review, Unset):
            service_stop_review = UNSET
        elif isinstance(self.service_stop_review, ServiceRunStopReview):
            service_stop_review = self.service_stop_review.to_dict()
        else:
            service_stop_review = self.service_stop_review

        workload_intent_ordinal: int | None | Unset
        if isinstance(self.workload_intent_ordinal, Unset):
            workload_intent_ordinal = UNSET
        else:
            workload_intent_ordinal = self.workload_intent_ordinal


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "owner_id": owner_id,
            "owner_kind": owner_kind,
            "plan_digest": plan_digest,
            "schema_version": schema_version,
        })
        if execution_mode is not UNSET:
            field_dict["execution_mode"] = execution_mode
        if job_run_stop_authorization is not UNSET:
            field_dict["job_run_stop_authorization"] = job_run_stop_authorization
        if phases is not UNSET:
            field_dict["phases"] = phases
        if profile_partial_stop is not UNSET:
            field_dict["profile_partial_stop"] = profile_partial_stop
        if recovery is not UNSET:
            field_dict["recovery"] = recovery
        if service_stop_review is not UNSET:
            field_dict["service_stop_review"] = service_stop_review
        if workload_intent_ordinal is not UNSET:
            field_dict["workload_intent_ordinal"] = workload_intent_ordinal

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.distributed_recovery_marker import DistributedRecoveryMarker # noqa: PLC0415
        from ..models.job_run_stop_scope import JobRunStopScope # noqa: PLC0415
        from ..models.profile_partial_stop import ProfilePartialStop # noqa: PLC0415
        from ..models.service_run_stop_review import ServiceRunStopReview # noqa: PLC0415
        from ..models.stop_phase_operation import StopPhaseOperation # noqa: PLC0415
        d = dict(src_dict)
        owner_id = d.pop("owner_id")

        owner_kind = check_recipe_stop_parent_owner_kind(d.pop("owner_kind"))




        plan_digest = d.pop("plan_digest")

        schema_version = cast(Literal[1] , d.pop("schema_version"))
        if schema_version != 1:
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        def _parse_execution_mode(data: object) -> Literal['one-shot-jobs'] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            execution_mode_type_0 = cast(Literal['one-shot-jobs'] , data)
            if execution_mode_type_0 != 'one-shot-jobs':
                raise ValueError(f"execution_mode_type_0 must match const 'one-shot-jobs', got '{execution_mode_type_0}'")
            return execution_mode_type_0
            return cast(Literal['one-shot-jobs'] | None | Unset, data)

        execution_mode = _parse_execution_mode(d.pop("execution_mode", UNSET))


        def _parse_job_run_stop_authorization(data: object) -> JobRunStopScope | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                job_run_stop_authorization_type_0 = JobRunStopScope.from_dict(data)



                return job_run_stop_authorization_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(JobRunStopScope | None | Unset, data)

        job_run_stop_authorization = _parse_job_run_stop_authorization(d.pop("job_run_stop_authorization", UNSET))


        def _parse_phases(data: object) -> list[list[StopPhaseOperation]] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                phases_type_0 = []
                _phases_type_0 = data
                for phases_type_0_item_data in (_phases_type_0):
                    phases_type_0_item = []
                    _phases_type_0_item = phases_type_0_item_data
                    for phases_type_0_item_item_data in (_phases_type_0_item):
                        phases_type_0_item_item = StopPhaseOperation.from_dict(phases_type_0_item_item_data)



                        phases_type_0_item.append(phases_type_0_item_item)

                    phases_type_0.append(phases_type_0_item)

                return phases_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[list[StopPhaseOperation]] | None | Unset, data)

        phases = _parse_phases(d.pop("phases", UNSET))


        def _parse_profile_partial_stop(data: object) -> None | ProfilePartialStop | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                profile_partial_stop_type_0 = ProfilePartialStop.from_dict(data)



                return profile_partial_stop_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ProfilePartialStop | Unset, data)

        profile_partial_stop = _parse_profile_partial_stop(d.pop("profile_partial_stop", UNSET))


        def _parse_recovery(data: object) -> DistributedRecoveryMarker | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                recovery_type_0 = DistributedRecoveryMarker.from_dict(data)



                return recovery_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(DistributedRecoveryMarker | None | Unset, data)

        recovery = _parse_recovery(d.pop("recovery", UNSET))


        def _parse_service_stop_review(data: object) -> None | ServiceRunStopReview | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                service_stop_review_type_0 = ServiceRunStopReview.from_dict(data)



                return service_stop_review_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ServiceRunStopReview | Unset, data)

        service_stop_review = _parse_service_stop_review(d.pop("service_stop_review", UNSET))


        def _parse_workload_intent_ordinal(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        workload_intent_ordinal = _parse_workload_intent_ordinal(d.pop("workload_intent_ordinal", UNSET))


        recipe_stop_parent = cls(
            owner_id=owner_id,
            owner_kind=owner_kind,
            plan_digest=plan_digest,
            schema_version=schema_version,
            execution_mode=execution_mode,
            job_run_stop_authorization=job_run_stop_authorization,
            phases=phases,
            profile_partial_stop=profile_partial_stop,
            recovery=recovery,
            service_stop_review=service_stop_review,
            workload_intent_ordinal=workload_intent_ordinal,
        )

        return recipe_stop_parent
