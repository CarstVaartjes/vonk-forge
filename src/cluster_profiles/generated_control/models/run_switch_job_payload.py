from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.run_switch_job_payload_action import check_run_switch_job_payload_action
from ..models.run_switch_job_payload_action import RunSwitchJobPayloadAction
from ..models.run_switch_job_payload_operation_kind import check_run_switch_job_payload_operation_kind
from ..models.run_switch_job_payload_operation_kind import RunSwitchJobPayloadOperationKind
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.run_switch_cleanup_intent import RunSwitchCleanupIntent
  from ..models.run_switch_operation_result import RunSwitchOperationResult
  from ..models.run_switch_plan import RunSwitchPlan
  from ..models.run_switch_profile_stop_intent import RunSwitchProfileStopIntent
  from ..models.run_switch_run_intent import RunSwitchRunIntent
  from ..models.run_switch_stop_intent import RunSwitchStopIntent





T = TypeVar("T", bound="RunSwitchJobPayload")



@_attrs_define
class RunSwitchJobPayload:
    """ The reviewed plan, the request that asked for it and the live progress.

        Attributes:
            action (RunSwitchJobPayloadAction):
            operation_kind (RunSwitchJobPayloadOperationKind):
            plan (RunSwitchPlan):
            plan_digest (str):
            progress (RunSwitchOperationResult): Exact durable result tree stored in ``Job.result``.
            schema_version (Literal[2]):
            intent (None | RunSwitchCleanupIntent | RunSwitchProfileStopIntent | RunSwitchRunIntent | RunSwitchStopIntent |
                Unset):
            retry_of (None | str | Unset):
            workload_intent_ordinal (int | None | Unset):
     """

    action: RunSwitchJobPayloadAction
    operation_kind: RunSwitchJobPayloadOperationKind
    plan: RunSwitchPlan
    plan_digest: str
    progress: RunSwitchOperationResult
    schema_version: Literal[2]
    intent: None | RunSwitchCleanupIntent | RunSwitchProfileStopIntent | RunSwitchRunIntent | RunSwitchStopIntent | Unset = UNSET
    retry_of: None | str | Unset = UNSET
    workload_intent_ordinal: int | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.run_switch_cleanup_intent import RunSwitchCleanupIntent # noqa: PLC0415
        from ..models.run_switch_operation_result import RunSwitchOperationResult # noqa: PLC0415
        from ..models.run_switch_plan import RunSwitchPlan # noqa: PLC0415
        from ..models.run_switch_profile_stop_intent import RunSwitchProfileStopIntent # noqa: PLC0415
        from ..models.run_switch_run_intent import RunSwitchRunIntent # noqa: PLC0415
        from ..models.run_switch_stop_intent import RunSwitchStopIntent # noqa: PLC0415
        action: str = self.action

        operation_kind: str = self.operation_kind

        plan = self.plan.to_dict()

        plan_digest = self.plan_digest

        progress = self.progress.to_dict()

        schema_version = self.schema_version

        intent: dict[str, Any] | None | Unset
        if isinstance(self.intent, Unset):
            intent = UNSET
        elif isinstance(self.intent, RunSwitchRunIntent):
            intent = self.intent.to_dict()
        elif isinstance(self.intent, RunSwitchStopIntent):
            intent = self.intent.to_dict()
        elif isinstance(self.intent, RunSwitchCleanupIntent):
            intent = self.intent.to_dict()
        elif isinstance(self.intent, RunSwitchProfileStopIntent):
            intent = self.intent.to_dict()
        else:
            intent = self.intent

        retry_of: None | str | Unset
        if isinstance(self.retry_of, Unset):
            retry_of = UNSET
        else:
            retry_of = self.retry_of

        workload_intent_ordinal: int | None | Unset
        if isinstance(self.workload_intent_ordinal, Unset):
            workload_intent_ordinal = UNSET
        else:
            workload_intent_ordinal = self.workload_intent_ordinal


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "action": action,
            "operation_kind": operation_kind,
            "plan": plan,
            "plan_digest": plan_digest,
            "progress": progress,
            "schema_version": schema_version,
        })
        if intent is not UNSET:
            field_dict["intent"] = intent
        if retry_of is not UNSET:
            field_dict["retry_of"] = retry_of
        if workload_intent_ordinal is not UNSET:
            field_dict["workload_intent_ordinal"] = workload_intent_ordinal

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.run_switch_cleanup_intent import RunSwitchCleanupIntent # noqa: PLC0415
        from ..models.run_switch_operation_result import RunSwitchOperationResult # noqa: PLC0415
        from ..models.run_switch_plan import RunSwitchPlan # noqa: PLC0415
        from ..models.run_switch_profile_stop_intent import RunSwitchProfileStopIntent # noqa: PLC0415
        from ..models.run_switch_run_intent import RunSwitchRunIntent # noqa: PLC0415
        from ..models.run_switch_stop_intent import RunSwitchStopIntent # noqa: PLC0415
        d = dict(src_dict)
        action = check_run_switch_job_payload_action(d.pop("action"))




        operation_kind = check_run_switch_job_payload_operation_kind(d.pop("operation_kind"))




        plan = RunSwitchPlan.from_dict(d.pop("plan"))




        plan_digest = d.pop("plan_digest")

        progress = RunSwitchOperationResult.from_dict(d.pop("progress"))




        schema_version = cast(Literal[2] , d.pop("schema_version"))
        if schema_version != 2:
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        def _parse_intent(data: object) -> None | RunSwitchCleanupIntent | RunSwitchProfileStopIntent | RunSwitchRunIntent | RunSwitchStopIntent | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                intent_type_0_type_0 = RunSwitchRunIntent.from_dict(data)



                return intent_type_0_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                intent_type_0_type_1 = RunSwitchStopIntent.from_dict(data)



                return intent_type_0_type_1
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                intent_type_0_type_2 = RunSwitchCleanupIntent.from_dict(data)



                return intent_type_0_type_2
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                intent_type_0_type_3 = RunSwitchProfileStopIntent.from_dict(data)



                return intent_type_0_type_3
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RunSwitchCleanupIntent | RunSwitchProfileStopIntent | RunSwitchRunIntent | RunSwitchStopIntent | Unset, data)

        intent = _parse_intent(d.pop("intent", UNSET))


        def _parse_retry_of(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        retry_of = _parse_retry_of(d.pop("retry_of", UNSET))


        def _parse_workload_intent_ordinal(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        workload_intent_ordinal = _parse_workload_intent_ordinal(d.pop("workload_intent_ordinal", UNSET))


        run_switch_job_payload = cls(
            action=action,
            operation_kind=operation_kind,
            plan=plan,
            plan_digest=plan_digest,
            progress=progress,
            schema_version=schema_version,
            intent=intent,
            retry_of=retry_of,
            workload_intent_ordinal=workload_intent_ordinal,
        )

        return run_switch_job_payload
