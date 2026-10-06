from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.artifact_job_result_evidence_failure_kind_type_0 import ArtifactJobResultEvidenceFailureKindType0
from ..models.artifact_job_result_evidence_failure_kind_type_0 import check_artifact_job_result_evidence_failure_kind_type_0
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="ArtifactJobResultEvidence")



@_attrs_define
class ArtifactJobResultEvidence:
    """ What the Controller knows about how a job ended, to every field.

        Attributes:
            active_scope_may_remain (bool | None | Unset):
            cancel_actor (None | str | Unset):
            cancel_reason (None | str | Unset):
            cancel_request_id (None | str | Unset):
            elapsed_milliseconds (int | None | Unset):
            failure_kind (ArtifactJobResultEvidenceFailureKindType0 | None | Unset):
            late_results_accepted (bool | None | Unset):
            peak_memory_bytes (int | None | Unset):
            recoverable (bool | None | Unset):
            residue_resolved_by (Literal['exact-stop'] | None | Unset):
     """

    active_scope_may_remain: bool | None | Unset = UNSET
    cancel_actor: None | str | Unset = UNSET
    cancel_reason: None | str | Unset = UNSET
    cancel_request_id: None | str | Unset = UNSET
    elapsed_milliseconds: int | None | Unset = UNSET
    failure_kind: ArtifactJobResultEvidenceFailureKindType0 | None | Unset = UNSET
    late_results_accepted: bool | None | Unset = UNSET
    peak_memory_bytes: int | None | Unset = UNSET
    recoverable: bool | None | Unset = UNSET
    residue_resolved_by: Literal['exact-stop'] | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        active_scope_may_remain: bool | None | Unset
        if isinstance(self.active_scope_may_remain, Unset):
            active_scope_may_remain = UNSET
        else:
            active_scope_may_remain = self.active_scope_may_remain

        cancel_actor: None | str | Unset
        if isinstance(self.cancel_actor, Unset):
            cancel_actor = UNSET
        else:
            cancel_actor = self.cancel_actor

        cancel_reason: None | str | Unset
        if isinstance(self.cancel_reason, Unset):
            cancel_reason = UNSET
        else:
            cancel_reason = self.cancel_reason

        cancel_request_id: None | str | Unset
        if isinstance(self.cancel_request_id, Unset):
            cancel_request_id = UNSET
        else:
            cancel_request_id = self.cancel_request_id

        elapsed_milliseconds: int | None | Unset
        if isinstance(self.elapsed_milliseconds, Unset):
            elapsed_milliseconds = UNSET
        else:
            elapsed_milliseconds = self.elapsed_milliseconds

        failure_kind: None | str | Unset
        if isinstance(self.failure_kind, Unset):
            failure_kind = UNSET
        elif isinstance(self.failure_kind, str):
            failure_kind = self.failure_kind
        else:
            failure_kind = self.failure_kind

        late_results_accepted: bool | None | Unset
        if isinstance(self.late_results_accepted, Unset):
            late_results_accepted = UNSET
        else:
            late_results_accepted = self.late_results_accepted

        peak_memory_bytes: int | None | Unset
        if isinstance(self.peak_memory_bytes, Unset):
            peak_memory_bytes = UNSET
        else:
            peak_memory_bytes = self.peak_memory_bytes

        recoverable: bool | None | Unset
        if isinstance(self.recoverable, Unset):
            recoverable = UNSET
        else:
            recoverable = self.recoverable

        residue_resolved_by: Literal['exact-stop'] | None | Unset
        if isinstance(self.residue_resolved_by, Unset):
            residue_resolved_by = UNSET
        else:
            residue_resolved_by = self.residue_resolved_by


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if active_scope_may_remain is not UNSET:
            field_dict["active_scope_may_remain"] = active_scope_may_remain
        if cancel_actor is not UNSET:
            field_dict["cancel_actor"] = cancel_actor
        if cancel_reason is not UNSET:
            field_dict["cancel_reason"] = cancel_reason
        if cancel_request_id is not UNSET:
            field_dict["cancel_request_id"] = cancel_request_id
        if elapsed_milliseconds is not UNSET:
            field_dict["elapsed_milliseconds"] = elapsed_milliseconds
        if failure_kind is not UNSET:
            field_dict["failure_kind"] = failure_kind
        if late_results_accepted is not UNSET:
            field_dict["late_results_accepted"] = late_results_accepted
        if peak_memory_bytes is not UNSET:
            field_dict["peak_memory_bytes"] = peak_memory_bytes
        if recoverable is not UNSET:
            field_dict["recoverable"] = recoverable
        if residue_resolved_by is not UNSET:
            field_dict["residue_resolved_by"] = residue_resolved_by

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        def _parse_active_scope_may_remain(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        active_scope_may_remain = _parse_active_scope_may_remain(d.pop("active_scope_may_remain", UNSET))


        def _parse_cancel_actor(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        cancel_actor = _parse_cancel_actor(d.pop("cancel_actor", UNSET))


        def _parse_cancel_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        cancel_reason = _parse_cancel_reason(d.pop("cancel_reason", UNSET))


        def _parse_cancel_request_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        cancel_request_id = _parse_cancel_request_id(d.pop("cancel_request_id", UNSET))


        def _parse_elapsed_milliseconds(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        elapsed_milliseconds = _parse_elapsed_milliseconds(d.pop("elapsed_milliseconds", UNSET))


        def _parse_failure_kind(data: object) -> ArtifactJobResultEvidenceFailureKindType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                failure_kind_type_0 = check_artifact_job_result_evidence_failure_kind_type_0(data)



                return failure_kind_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ArtifactJobResultEvidenceFailureKindType0 | None | Unset, data)

        failure_kind = _parse_failure_kind(d.pop("failure_kind", UNSET))


        def _parse_late_results_accepted(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        late_results_accepted = _parse_late_results_accepted(d.pop("late_results_accepted", UNSET))


        def _parse_peak_memory_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        peak_memory_bytes = _parse_peak_memory_bytes(d.pop("peak_memory_bytes", UNSET))


        def _parse_recoverable(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        recoverable = _parse_recoverable(d.pop("recoverable", UNSET))


        def _parse_residue_resolved_by(data: object) -> Literal['exact-stop'] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            residue_resolved_by_type_0 = cast(Literal['exact-stop'] , data)
            if residue_resolved_by_type_0 != 'exact-stop':
                raise ValueError(f"residue_resolved_by_type_0 must match const 'exact-stop', got '{residue_resolved_by_type_0}'")
            return residue_resolved_by_type_0
            return cast(Literal['exact-stop'] | None | Unset, data)

        residue_resolved_by = _parse_residue_resolved_by(d.pop("residue_resolved_by", UNSET))


        artifact_job_result_evidence = cls(
            active_scope_may_remain=active_scope_may_remain,
            cancel_actor=cancel_actor,
            cancel_reason=cancel_reason,
            cancel_request_id=cancel_request_id,
            elapsed_milliseconds=elapsed_milliseconds,
            failure_kind=failure_kind,
            late_results_accepted=late_results_accepted,
            peak_memory_bytes=peak_memory_bytes,
            recoverable=recoverable,
            residue_resolved_by=residue_resolved_by,
        )

        return artifact_job_result_evidence
