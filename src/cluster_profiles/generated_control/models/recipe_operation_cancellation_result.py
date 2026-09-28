from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast
import datetime

if TYPE_CHECKING:
  from ..models.recipe_operation_cancellation_result_launch_evidence_type_0 import RecipeOperationCancellationResultLaunchEvidenceType0
  from ..models.recipe_operation_cancellation_result_node_evidence_type_0 import RecipeOperationCancellationResultNodeEvidenceType0





T = TypeVar("T", bound="RecipeOperationCancellationResult")



@_attrs_define
class RecipeOperationCancellationResult:
    """ Cancellation metadata merged into a pending lifecycle result.

        Attributes:
            cancel_actor (str):
            cancel_request_id (str):
            cancel_requested (bool):
            reason (str):
            cancel_requested_at (datetime.datetime | None | Unset):
            cancelled (bool | None | Unset):
            launch_evidence (None | RecipeOperationCancellationResultLaunchEvidenceType0 | Unset):
            node_evidence (None | RecipeOperationCancellationResultNodeEvidenceType0 | Unset):
            recovery (Literal['retry creates a new operation'] | None | Unset):
     """

    cancel_actor: str
    cancel_request_id: str
    cancel_requested: bool
    reason: str
    cancel_requested_at: datetime.datetime | None | Unset = UNSET
    cancelled: bool | None | Unset = UNSET
    launch_evidence: None | RecipeOperationCancellationResultLaunchEvidenceType0 | Unset = UNSET
    node_evidence: None | RecipeOperationCancellationResultNodeEvidenceType0 | Unset = UNSET
    recovery: Literal['retry creates a new operation'] | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_operation_cancellation_result_launch_evidence_type_0 import RecipeOperationCancellationResultLaunchEvidenceType0 # noqa: PLC0415
        from ..models.recipe_operation_cancellation_result_node_evidence_type_0 import RecipeOperationCancellationResultNodeEvidenceType0 # noqa: PLC0415
        cancel_actor = self.cancel_actor

        cancel_request_id = self.cancel_request_id

        cancel_requested = self.cancel_requested

        reason = self.reason

        cancel_requested_at: None | str | Unset
        if isinstance(self.cancel_requested_at, Unset):
            cancel_requested_at = UNSET
        elif isinstance(self.cancel_requested_at, datetime.datetime):
            cancel_requested_at = self.cancel_requested_at.isoformat()
        else:
            cancel_requested_at = self.cancel_requested_at

        cancelled: bool | None | Unset
        if isinstance(self.cancelled, Unset):
            cancelled = UNSET
        else:
            cancelled = self.cancelled

        launch_evidence: dict[str, Any] | None | Unset
        if isinstance(self.launch_evidence, Unset):
            launch_evidence = UNSET
        elif isinstance(self.launch_evidence, RecipeOperationCancellationResultLaunchEvidenceType0):
            launch_evidence = self.launch_evidence.to_dict()
        else:
            launch_evidence = self.launch_evidence

        node_evidence: dict[str, Any] | None | Unset
        if isinstance(self.node_evidence, Unset):
            node_evidence = UNSET
        elif isinstance(self.node_evidence, RecipeOperationCancellationResultNodeEvidenceType0):
            node_evidence = self.node_evidence.to_dict()
        else:
            node_evidence = self.node_evidence

        recovery: Literal['retry creates a new operation'] | None | Unset
        if isinstance(self.recovery, Unset):
            recovery = UNSET
        else:
            recovery = self.recovery


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "cancel_actor": cancel_actor,
            "cancel_request_id": cancel_request_id,
            "cancel_requested": cancel_requested,
            "reason": reason,
        })
        if cancel_requested_at is not UNSET:
            field_dict["cancel_requested_at"] = cancel_requested_at
        if cancelled is not UNSET:
            field_dict["cancelled"] = cancelled
        if launch_evidence is not UNSET:
            field_dict["launch_evidence"] = launch_evidence
        if node_evidence is not UNSET:
            field_dict["node_evidence"] = node_evidence
        if recovery is not UNSET:
            field_dict["recovery"] = recovery

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_operation_cancellation_result_launch_evidence_type_0 import RecipeOperationCancellationResultLaunchEvidenceType0 # noqa: PLC0415
        from ..models.recipe_operation_cancellation_result_node_evidence_type_0 import RecipeOperationCancellationResultNodeEvidenceType0 # noqa: PLC0415
        d = dict(src_dict)
        cancel_actor = d.pop("cancel_actor")

        cancel_request_id = d.pop("cancel_request_id")

        cancel_requested = d.pop("cancel_requested")

        reason = d.pop("reason")

        def _parse_cancel_requested_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                cancel_requested_at_type_0 = datetime.datetime.fromisoformat(data)



                return cancel_requested_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        cancel_requested_at = _parse_cancel_requested_at(d.pop("cancel_requested_at", UNSET))


        def _parse_cancelled(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        cancelled = _parse_cancelled(d.pop("cancelled", UNSET))


        def _parse_launch_evidence(data: object) -> None | RecipeOperationCancellationResultLaunchEvidenceType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                launch_evidence_type_0 = RecipeOperationCancellationResultLaunchEvidenceType0.from_dict(data)



                return launch_evidence_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RecipeOperationCancellationResultLaunchEvidenceType0 | Unset, data)

        launch_evidence = _parse_launch_evidence(d.pop("launch_evidence", UNSET))


        def _parse_node_evidence(data: object) -> None | RecipeOperationCancellationResultNodeEvidenceType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                node_evidence_type_0 = RecipeOperationCancellationResultNodeEvidenceType0.from_dict(data)



                return node_evidence_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RecipeOperationCancellationResultNodeEvidenceType0 | Unset, data)

        node_evidence = _parse_node_evidence(d.pop("node_evidence", UNSET))


        def _parse_recovery(data: object) -> Literal['retry creates a new operation'] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            recovery_type_0 = cast(Literal['retry creates a new operation'] , data)
            if recovery_type_0 != 'retry creates a new operation':
                raise ValueError(f"recovery_type_0 must match const 'retry creates a new operation', got '{recovery_type_0}'")
            return recovery_type_0
            return cast(Literal['retry creates a new operation'] | None | Unset, data)

        recovery = _parse_recovery(d.pop("recovery", UNSET))


        recipe_operation_cancellation_result = cls(
            cancel_actor=cancel_actor,
            cancel_request_id=cancel_request_id,
            cancel_requested=cancel_requested,
            reason=reason,
            cancel_requested_at=cancel_requested_at,
            cancelled=cancelled,
            launch_evidence=launch_evidence,
            node_evidence=node_evidence,
            recovery=recovery,
        )

        return recipe_operation_cancellation_result
