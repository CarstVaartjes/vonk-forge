from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.lifecycle_preflight_checkpoint_receipts import LifecyclePreflightCheckpointReceipts
  from ..models.lifecycle_preflight_checkpoint_attempts import LifecyclePreflightCheckpointAttempts





T = TypeVar("T", bound="LifecyclePreflightCheckpoint")



@_attrs_define
class LifecyclePreflightCheckpoint:
    """
        Attributes:
            phase_index (int):
            attempts (Union[Unset, LifecyclePreflightCheckpointAttempts]):
            pending_job_id (Union[None, Unset, str]):
            pending_node_id (Union[None, Unset, str]):
            receipts (Union[Unset, LifecyclePreflightCheckpointReceipts]):
     """

    phase_index: int
    attempts: Union[Unset, 'LifecyclePreflightCheckpointAttempts'] = UNSET
    pending_job_id: Union[None, Unset, str] = UNSET
    pending_node_id: Union[None, Unset, str] = UNSET
    receipts: Union[Unset, 'LifecyclePreflightCheckpointReceipts'] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.lifecycle_preflight_checkpoint_receipts import LifecyclePreflightCheckpointReceipts
        from ..models.lifecycle_preflight_checkpoint_attempts import LifecyclePreflightCheckpointAttempts
        phase_index = self.phase_index

        attempts: Union[Unset, dict[str, Any]] = UNSET
        if not isinstance(self.attempts, Unset):
            attempts = self.attempts.to_dict()

        pending_job_id: Union[None, Unset, str]
        if isinstance(self.pending_job_id, Unset):
            pending_job_id = UNSET
        else:
            pending_job_id = self.pending_job_id

        pending_node_id: Union[None, Unset, str]
        if isinstance(self.pending_node_id, Unset):
            pending_node_id = UNSET
        else:
            pending_node_id = self.pending_node_id

        receipts: Union[Unset, dict[str, Any]] = UNSET
        if not isinstance(self.receipts, Unset):
            receipts = self.receipts.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "phase_index": phase_index,
        })
        if attempts is not UNSET:
            field_dict["attempts"] = attempts
        if pending_job_id is not UNSET:
            field_dict["pending_job_id"] = pending_job_id
        if pending_node_id is not UNSET:
            field_dict["pending_node_id"] = pending_node_id
        if receipts is not UNSET:
            field_dict["receipts"] = receipts

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.lifecycle_preflight_checkpoint_receipts import LifecyclePreflightCheckpointReceipts
        from ..models.lifecycle_preflight_checkpoint_attempts import LifecyclePreflightCheckpointAttempts
        d = dict(src_dict)
        phase_index = d.pop("phase_index")

        _attempts = d.pop("attempts", UNSET)
        attempts: Union[Unset, LifecyclePreflightCheckpointAttempts]
        if isinstance(_attempts,  Unset):
            attempts = UNSET
        else:
            attempts = LifecyclePreflightCheckpointAttempts.from_dict(_attempts)




        def _parse_pending_job_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        pending_job_id = _parse_pending_job_id(d.pop("pending_job_id", UNSET))


        def _parse_pending_node_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        pending_node_id = _parse_pending_node_id(d.pop("pending_node_id", UNSET))


        _receipts = d.pop("receipts", UNSET)
        receipts: Union[Unset, LifecyclePreflightCheckpointReceipts]
        if isinstance(_receipts,  Unset):
            receipts = UNSET
        else:
            receipts = LifecyclePreflightCheckpointReceipts.from_dict(_receipts)




        lifecycle_preflight_checkpoint = cls(
            phase_index=phase_index,
            attempts=attempts,
            pending_job_id=pending_job_id,
            pending_node_id=pending_node_id,
            receipts=receipts,
        )

        return lifecycle_preflight_checkpoint
