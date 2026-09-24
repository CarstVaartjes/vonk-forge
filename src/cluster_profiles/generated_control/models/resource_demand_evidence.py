from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.resource_demand_evidence_evidence_state import check_resource_demand_evidence_evidence_state
from ..models.resource_demand_evidence_evidence_state import ResourceDemandEvidenceEvidenceState
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="ResourceDemandEvidence")



@_attrs_define
class ResourceDemandEvidence:
    """ The evidence terms used for one selected rank's memory fit.

        Attributes:
            evidence_state (ResourceDemandEvidenceEvidenceState):
            batch_bytes (Union[None, Unset, int]):
            concurrency_bytes (Union[None, Unset, int]):
            context_bytes (Union[None, Unset, int]):
            evidence_digest (Union[None, Unset, str]):
            runtime_overhead_bytes (Union[None, Unset, int]):
            total_bytes (Union[None, Unset, int]):
            weights_bytes (Union[None, Unset, int]):
     """

    evidence_state: ResourceDemandEvidenceEvidenceState
    batch_bytes: Union[None, Unset, int] = UNSET
    concurrency_bytes: Union[None, Unset, int] = UNSET
    context_bytes: Union[None, Unset, int] = UNSET
    evidence_digest: Union[None, Unset, str] = UNSET
    runtime_overhead_bytes: Union[None, Unset, int] = UNSET
    total_bytes: Union[None, Unset, int] = UNSET
    weights_bytes: Union[None, Unset, int] = UNSET





    def to_dict(self) -> dict[str, Any]:
        evidence_state: str = self.evidence_state

        batch_bytes: Union[None, Unset, int]
        if isinstance(self.batch_bytes, Unset):
            batch_bytes = UNSET
        else:
            batch_bytes = self.batch_bytes

        concurrency_bytes: Union[None, Unset, int]
        if isinstance(self.concurrency_bytes, Unset):
            concurrency_bytes = UNSET
        else:
            concurrency_bytes = self.concurrency_bytes

        context_bytes: Union[None, Unset, int]
        if isinstance(self.context_bytes, Unset):
            context_bytes = UNSET
        else:
            context_bytes = self.context_bytes

        evidence_digest: Union[None, Unset, str]
        if isinstance(self.evidence_digest, Unset):
            evidence_digest = UNSET
        else:
            evidence_digest = self.evidence_digest

        runtime_overhead_bytes: Union[None, Unset, int]
        if isinstance(self.runtime_overhead_bytes, Unset):
            runtime_overhead_bytes = UNSET
        else:
            runtime_overhead_bytes = self.runtime_overhead_bytes

        total_bytes: Union[None, Unset, int]
        if isinstance(self.total_bytes, Unset):
            total_bytes = UNSET
        else:
            total_bytes = self.total_bytes

        weights_bytes: Union[None, Unset, int]
        if isinstance(self.weights_bytes, Unset):
            weights_bytes = UNSET
        else:
            weights_bytes = self.weights_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "evidence_state": evidence_state,
        })
        if batch_bytes is not UNSET:
            field_dict["batch_bytes"] = batch_bytes
        if concurrency_bytes is not UNSET:
            field_dict["concurrency_bytes"] = concurrency_bytes
        if context_bytes is not UNSET:
            field_dict["context_bytes"] = context_bytes
        if evidence_digest is not UNSET:
            field_dict["evidence_digest"] = evidence_digest
        if runtime_overhead_bytes is not UNSET:
            field_dict["runtime_overhead_bytes"] = runtime_overhead_bytes
        if total_bytes is not UNSET:
            field_dict["total_bytes"] = total_bytes
        if weights_bytes is not UNSET:
            field_dict["weights_bytes"] = weights_bytes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        evidence_state = check_resource_demand_evidence_evidence_state(d.pop("evidence_state"))




        def _parse_batch_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        batch_bytes = _parse_batch_bytes(d.pop("batch_bytes", UNSET))


        def _parse_concurrency_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        concurrency_bytes = _parse_concurrency_bytes(d.pop("concurrency_bytes", UNSET))


        def _parse_context_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        context_bytes = _parse_context_bytes(d.pop("context_bytes", UNSET))


        def _parse_evidence_digest(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        evidence_digest = _parse_evidence_digest(d.pop("evidence_digest", UNSET))


        def _parse_runtime_overhead_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        runtime_overhead_bytes = _parse_runtime_overhead_bytes(d.pop("runtime_overhead_bytes", UNSET))


        def _parse_total_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        total_bytes = _parse_total_bytes(d.pop("total_bytes", UNSET))


        def _parse_weights_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        weights_bytes = _parse_weights_bytes(d.pop("weights_bytes", UNSET))


        resource_demand_evidence = cls(
            evidence_state=evidence_state,
            batch_bytes=batch_bytes,
            concurrency_bytes=concurrency_bytes,
            context_bytes=context_bytes,
            evidence_digest=evidence_digest,
            runtime_overhead_bytes=runtime_overhead_bytes,
            total_bytes=total_bytes,
            weights_bytes=weights_bytes,
        )

        return resource_demand_evidence
