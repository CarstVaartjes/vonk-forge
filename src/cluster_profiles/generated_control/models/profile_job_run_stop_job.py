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
  from ..models.offline_stop_intent import OfflineStopIntent
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
            profile_stop_authorization (ProfileJobRunStopAuthorization): Current accepted profile Stop owns this immutable
                JobRun scope.
            schema_version (Literal[1]):
            workload_intent_ordinal (int):
            offline_stop_intent (None | OfflineStopIntent | Unset):
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
    offline_stop_intent: None | OfflineStopIntent | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.offline_stop_intent import OfflineStopIntent # noqa: PLC0415
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

        offline_stop_intent: dict[str, Any] | None | Unset
        if isinstance(self.offline_stop_intent, Unset):
            offline_stop_intent = UNSET
        elif isinstance(self.offline_stop_intent, OfflineStopIntent):
            offline_stop_intent = self.offline_stop_intent.to_dict()
        else:
            offline_stop_intent = self.offline_stop_intent


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
        if offline_stop_intent is not UNSET:
            field_dict["offline_stop_intent"] = offline_stop_intent

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.offline_stop_intent import OfflineStopIntent # noqa: PLC0415
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

        def _parse_offline_stop_intent(data: object) -> None | OfflineStopIntent | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                offline_stop_intent_type_0 = OfflineStopIntent.from_dict(data)



                return offline_stop_intent_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | OfflineStopIntent | Unset, data)

        offline_stop_intent = _parse_offline_stop_intent(d.pop("offline_stop_intent", UNSET))


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
            offline_stop_intent=offline_stop_intent,
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
