from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.recipe_operation_cancellation_result_node_evidence_type_0 import RecipeOperationCancellationResultNodeEvidenceType0
  from ..models.recipe_operation_cancellation_result_launch_evidence_type_0 import RecipeOperationCancellationResultLaunchEvidenceType0





T = TypeVar("T", bound="RecipeOperationCancellationResult")



@_attrs_define
class RecipeOperationCancellationResult:
    """ Cancellation metadata merged into a pending lifecycle result.

        Attributes:
            cancel_actor (str):
            cancel_request_id (str):
            cancel_requested (bool):
            reason (str):
            cancelled (Union[None, Unset, bool]):
            launch_evidence (Union['RecipeOperationCancellationResultLaunchEvidenceType0', None, Unset]):
            node_evidence (Union['RecipeOperationCancellationResultNodeEvidenceType0', None, Unset]):
            recovery (Union[Literal['retry creates a new operation'], None, Unset]):
     """

    cancel_actor: str
    cancel_request_id: str
    cancel_requested: bool
    reason: str
    cancelled: Union[None, Unset, bool] = UNSET
    launch_evidence: Union['RecipeOperationCancellationResultLaunchEvidenceType0', None, Unset] = UNSET
    node_evidence: Union['RecipeOperationCancellationResultNodeEvidenceType0', None, Unset] = UNSET
    recovery: Union[Literal['retry creates a new operation'], None, Unset] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_operation_cancellation_result_node_evidence_type_0 import RecipeOperationCancellationResultNodeEvidenceType0
        from ..models.recipe_operation_cancellation_result_launch_evidence_type_0 import RecipeOperationCancellationResultLaunchEvidenceType0
        cancel_actor = self.cancel_actor

        cancel_request_id = self.cancel_request_id

        cancel_requested = self.cancel_requested

        reason = self.reason

        cancelled: Union[None, Unset, bool]
        if isinstance(self.cancelled, Unset):
            cancelled = UNSET
        else:
            cancelled = self.cancelled

        launch_evidence: Union[None, Unset, dict[str, Any]]
        if isinstance(self.launch_evidence, Unset):
            launch_evidence = UNSET
        elif isinstance(self.launch_evidence, RecipeOperationCancellationResultLaunchEvidenceType0):
            launch_evidence = self.launch_evidence.to_dict()
        else:
            launch_evidence = self.launch_evidence

        node_evidence: Union[None, Unset, dict[str, Any]]
        if isinstance(self.node_evidence, Unset):
            node_evidence = UNSET
        elif isinstance(self.node_evidence, RecipeOperationCancellationResultNodeEvidenceType0):
            node_evidence = self.node_evidence.to_dict()
        else:
            node_evidence = self.node_evidence

        recovery: Union[Literal['retry creates a new operation'], None, Unset]
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
        from ..models.recipe_operation_cancellation_result_node_evidence_type_0 import RecipeOperationCancellationResultNodeEvidenceType0
        from ..models.recipe_operation_cancellation_result_launch_evidence_type_0 import RecipeOperationCancellationResultLaunchEvidenceType0
        d = dict(src_dict)
        cancel_actor = d.pop("cancel_actor")

        cancel_request_id = d.pop("cancel_request_id")

        cancel_requested = d.pop("cancel_requested")

        reason = d.pop("reason")

        def _parse_cancelled(data: object) -> Union[None, Unset, bool]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, bool], data)

        cancelled = _parse_cancelled(d.pop("cancelled", UNSET))


        def _parse_launch_evidence(data: object) -> Union['RecipeOperationCancellationResultLaunchEvidenceType0', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                launch_evidence_type_0 = RecipeOperationCancellationResultLaunchEvidenceType0.from_dict(data)



                return launch_evidence_type_0
            except: # noqa: E722
                pass
            return cast(Union['RecipeOperationCancellationResultLaunchEvidenceType0', None, Unset], data)

        launch_evidence = _parse_launch_evidence(d.pop("launch_evidence", UNSET))


        def _parse_node_evidence(data: object) -> Union['RecipeOperationCancellationResultNodeEvidenceType0', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                node_evidence_type_0 = RecipeOperationCancellationResultNodeEvidenceType0.from_dict(data)



                return node_evidence_type_0
            except: # noqa: E722
                pass
            return cast(Union['RecipeOperationCancellationResultNodeEvidenceType0', None, Unset], data)

        node_evidence = _parse_node_evidence(d.pop("node_evidence", UNSET))


        def _parse_recovery(data: object) -> Union[Literal['retry creates a new operation'], None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            recovery_type_0 = cast(Literal['retry creates a new operation'] , data)
            if recovery_type_0 != 'retry creates a new operation':
                raise ValueError(f"recovery_type_0 must match const 'retry creates a new operation', got '{recovery_type_0}'")
            return recovery_type_0
            return cast(Union[Literal['retry creates a new operation'], None, Unset], data)

        recovery = _parse_recovery(d.pop("recovery", UNSET))


        recipe_operation_cancellation_result = cls(
            cancel_actor=cancel_actor,
            cancel_request_id=cancel_request_id,
            cancel_requested=cancel_requested,
            reason=reason,
            cancelled=cancelled,
            launch_evidence=launch_evidence,
            node_evidence=node_evidence,
            recovery=recovery,
        )

        return recipe_operation_cancellation_result
