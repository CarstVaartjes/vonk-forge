from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.profile_job_run_stop_authorization import ProfileJobRunStopAuthorization
  from ..models.profile_job_run_stop_phase_item import ProfileJobRunStopPhaseItem





T = TypeVar("T", bound="ProfileJobRunStopJob")



@_attrs_define
class ProfileJobRunStopJob:
    """ The one-shot Stop parent persisted for a current profile owner.

        Attributes:
            execution_mode (Literal['profile-jobrun-stop']):
            owner_id (str):
            owner_kind (Literal['run']):
            phases (list[list[ProfileJobRunStopPhaseItem]]):
            plan_digest (str):
            profile_application_id (str):
            profile_operation_id (str):
            profile_stop_authorization (ProfileJobRunStopAuthorization): Current accepted profile Stop and exact older one-
                shot effect.
            schema_version (Literal[1]):
            workload_intent_ordinal (int):
     """

    execution_mode: Literal['profile-jobrun-stop']
    owner_id: str
    owner_kind: Literal['run']
    phases: list[list[ProfileJobRunStopPhaseItem]]
    plan_digest: str
    profile_application_id: str
    profile_operation_id: str
    profile_stop_authorization: ProfileJobRunStopAuthorization
    schema_version: Literal[1]
    workload_intent_ordinal: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.profile_job_run_stop_authorization import ProfileJobRunStopAuthorization # noqa: PLC0415
        from ..models.profile_job_run_stop_phase_item import ProfileJobRunStopPhaseItem # noqa: PLC0415
        execution_mode = self.execution_mode

        owner_id = self.owner_id

        owner_kind = self.owner_kind

        phases = []
        for phases_item_data in self.phases:
            phases_item = []
            for phases_item_item_data in phases_item_data:
                phases_item_item = phases_item_item_data.to_dict()
                phases_item.append(phases_item_item)


            phases.append(phases_item)



        plan_digest = self.plan_digest

        profile_application_id = self.profile_application_id

        profile_operation_id = self.profile_operation_id

        profile_stop_authorization = self.profile_stop_authorization.to_dict()

        schema_version = self.schema_version

        workload_intent_ordinal = self.workload_intent_ordinal


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "execution_mode": execution_mode,
            "owner_id": owner_id,
            "owner_kind": owner_kind,
            "phases": phases,
            "plan_digest": plan_digest,
            "profile_application_id": profile_application_id,
            "profile_operation_id": profile_operation_id,
            "profile_stop_authorization": profile_stop_authorization,
            "schema_version": schema_version,
            "workload_intent_ordinal": workload_intent_ordinal,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.profile_job_run_stop_authorization import ProfileJobRunStopAuthorization # noqa: PLC0415
        from ..models.profile_job_run_stop_phase_item import ProfileJobRunStopPhaseItem # noqa: PLC0415
        d = dict(src_dict)
        execution_mode = cast(Literal['profile-jobrun-stop'] , d.pop("execution_mode"))
        if execution_mode != 'profile-jobrun-stop':
            raise ValueError(f"execution_mode must match const 'profile-jobrun-stop', got '{execution_mode}'")

        owner_id = d.pop("owner_id")

        owner_kind = cast(Literal['run'] , d.pop("owner_kind"))
        if owner_kind != 'run':
            raise ValueError(f"owner_kind must match const 'run', got '{owner_kind}'")

        phases = []
        _phases = d.pop("phases")
        for phases_item_data in (_phases):
            phases_item = []
            _phases_item = phases_item_data
            for phases_item_item_data in (_phases_item):
                phases_item_item = ProfileJobRunStopPhaseItem.from_dict(phases_item_item_data)



                phases_item.append(phases_item_item)

            phases.append(phases_item)


        plan_digest = d.pop("plan_digest")

        profile_application_id = d.pop("profile_application_id")

        profile_operation_id = d.pop("profile_operation_id")

        profile_stop_authorization = ProfileJobRunStopAuthorization.from_dict(d.pop("profile_stop_authorization"))




        schema_version = cast(Literal[1] , d.pop("schema_version"))
        if schema_version != 1:
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        workload_intent_ordinal = d.pop("workload_intent_ordinal")

        profile_job_run_stop_job = cls(
            execution_mode=execution_mode,
            owner_id=owner_id,
            owner_kind=owner_kind,
            phases=phases,
            plan_digest=plan_digest,
            profile_application_id=profile_application_id,
            profile_operation_id=profile_operation_id,
            profile_stop_authorization=profile_stop_authorization,
            schema_version=schema_version,
            workload_intent_ordinal=workload_intent_ordinal,
        )


        profile_job_run_stop_job.additional_properties = d
        return profile_job_run_stop_job

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
