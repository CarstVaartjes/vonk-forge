from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_start_parent_owner_kind import check_recipe_start_parent_owner_kind
from ..models.recipe_start_parent_owner_kind import RecipeStartParentOwnerKind
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast
import datetime

if TYPE_CHECKING:
  from ..models.distributed_recovery_marker import DistributedRecoveryMarker
  from ..models.start_phase_operation import StartPhaseOperation





T = TypeVar("T", bound="RecipeStartParent")



@_attrs_define
class RecipeStartParent:
    """
        Attributes:
            owner_id (str):
            owner_kind (RecipeStartParentOwnerKind):
            plan_digest (str):
            schema_version (Literal[1]):
            execution_mode (Literal['one-shot-jobs'] | None | Unset):
            phases (list[list[StartPhaseOperation]] | None | Unset):
            recovery (DistributedRecoveryMarker | None | Unset):
            start_anchored_at (datetime.datetime | None | Unset):
            start_deadline (datetime.datetime | None | Unset):
            workload_intent_ordinal (int | None | Unset):
     """

    owner_id: str
    owner_kind: RecipeStartParentOwnerKind
    plan_digest: str
    schema_version: Literal[1]
    execution_mode: Literal['one-shot-jobs'] | None | Unset = UNSET
    phases: list[list[StartPhaseOperation]] | None | Unset = UNSET
    recovery: DistributedRecoveryMarker | None | Unset = UNSET
    start_anchored_at: datetime.datetime | None | Unset = UNSET
    start_deadline: datetime.datetime | None | Unset = UNSET
    workload_intent_ordinal: int | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.distributed_recovery_marker import DistributedRecoveryMarker # noqa: PLC0415
        from ..models.start_phase_operation import StartPhaseOperation # noqa: PLC0415
        owner_id = self.owner_id

        owner_kind: str = self.owner_kind

        plan_digest = self.plan_digest

        schema_version = self.schema_version

        execution_mode: Literal['one-shot-jobs'] | None | Unset
        if isinstance(self.execution_mode, Unset):
            execution_mode = UNSET
        else:
            execution_mode = self.execution_mode

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

        recovery: dict[str, Any] | None | Unset
        if isinstance(self.recovery, Unset):
            recovery = UNSET
        elif isinstance(self.recovery, DistributedRecoveryMarker):
            recovery = self.recovery.to_dict()
        else:
            recovery = self.recovery

        start_anchored_at: None | str | Unset
        if isinstance(self.start_anchored_at, Unset):
            start_anchored_at = UNSET
        elif isinstance(self.start_anchored_at, datetime.datetime):
            start_anchored_at = self.start_anchored_at.isoformat()
        else:
            start_anchored_at = self.start_anchored_at

        start_deadline: None | str | Unset
        if isinstance(self.start_deadline, Unset):
            start_deadline = UNSET
        elif isinstance(self.start_deadline, datetime.datetime):
            start_deadline = self.start_deadline.isoformat()
        else:
            start_deadline = self.start_deadline

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
        if phases is not UNSET:
            field_dict["phases"] = phases
        if recovery is not UNSET:
            field_dict["recovery"] = recovery
        if start_anchored_at is not UNSET:
            field_dict["start_anchored_at"] = start_anchored_at
        if start_deadline is not UNSET:
            field_dict["start_deadline"] = start_deadline
        if workload_intent_ordinal is not UNSET:
            field_dict["workload_intent_ordinal"] = workload_intent_ordinal

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.distributed_recovery_marker import DistributedRecoveryMarker # noqa: PLC0415
        from ..models.start_phase_operation import StartPhaseOperation # noqa: PLC0415
        d = dict(src_dict)
        owner_id = d.pop("owner_id")

        owner_kind = check_recipe_start_parent_owner_kind(d.pop("owner_kind"))




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


        def _parse_phases(data: object) -> list[list[StartPhaseOperation]] | None | Unset:
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
                        phases_type_0_item_item = StartPhaseOperation.from_dict(phases_type_0_item_item_data)



                        phases_type_0_item.append(phases_type_0_item_item)

                    phases_type_0.append(phases_type_0_item)

                return phases_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[list[StartPhaseOperation]] | None | Unset, data)

        phases = _parse_phases(d.pop("phases", UNSET))


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


        def _parse_start_anchored_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                start_anchored_at_type_0 = datetime.datetime.fromisoformat(data)



                return start_anchored_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        start_anchored_at = _parse_start_anchored_at(d.pop("start_anchored_at", UNSET))


        def _parse_start_deadline(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                start_deadline_type_0 = datetime.datetime.fromisoformat(data)



                return start_deadline_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        start_deadline = _parse_start_deadline(d.pop("start_deadline", UNSET))


        def _parse_workload_intent_ordinal(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        workload_intent_ordinal = _parse_workload_intent_ordinal(d.pop("workload_intent_ordinal", UNSET))


        recipe_start_parent = cls(
            owner_id=owner_id,
            owner_kind=owner_kind,
            plan_digest=plan_digest,
            schema_version=schema_version,
            execution_mode=execution_mode,
            phases=phases,
            recovery=recovery,
            start_anchored_at=start_anchored_at,
            start_deadline=start_deadline,
            workload_intent_ordinal=workload_intent_ordinal,
        )

        return recipe_start_parent
