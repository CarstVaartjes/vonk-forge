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
  from ..models.failure_diagnostics import FailureDiagnostics





T = TypeVar("T", bound="RunNodePayload")



@_attrs_define
class RunNodePayload:
    """
        Attributes:
            entity_id (str):
            entity_kind (Literal['run-node']):
            node_id (str):
            rank (int):
            reserved_memory_bytes (int):
            role (str):
            run_id (str):
            state (str):
            failure_diagnostics (FailureDiagnostics | None | Unset):
            observed_memory_bytes (int | None | Unset):
     """

    entity_id: str
    entity_kind: Literal['run-node']
    node_id: str
    rank: int
    reserved_memory_bytes: int
    role: str
    run_id: str
    state: str
    failure_diagnostics: FailureDiagnostics | None | Unset = UNSET
    observed_memory_bytes: int | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.failure_diagnostics import FailureDiagnostics # noqa: PLC0415
        entity_id = self.entity_id

        entity_kind = self.entity_kind

        node_id = self.node_id

        rank = self.rank

        reserved_memory_bytes = self.reserved_memory_bytes

        role = self.role

        run_id = self.run_id

        state = self.state

        failure_diagnostics: dict[str, Any] | None | Unset
        if isinstance(self.failure_diagnostics, Unset):
            failure_diagnostics = UNSET
        elif isinstance(self.failure_diagnostics, FailureDiagnostics):
            failure_diagnostics = self.failure_diagnostics.to_dict()
        else:
            failure_diagnostics = self.failure_diagnostics

        observed_memory_bytes: int | None | Unset
        if isinstance(self.observed_memory_bytes, Unset):
            observed_memory_bytes = UNSET
        else:
            observed_memory_bytes = self.observed_memory_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "entity_id": entity_id,
            "entity_kind": entity_kind,
            "node_id": node_id,
            "rank": rank,
            "reserved_memory_bytes": reserved_memory_bytes,
            "role": role,
            "run_id": run_id,
            "state": state,
        })
        if failure_diagnostics is not UNSET:
            field_dict["failure_diagnostics"] = failure_diagnostics
        if observed_memory_bytes is not UNSET:
            field_dict["observed_memory_bytes"] = observed_memory_bytes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.failure_diagnostics import FailureDiagnostics # noqa: PLC0415
        d = dict(src_dict)
        entity_id = d.pop("entity_id")

        entity_kind = cast(Literal['run-node'] , d.pop("entity_kind"))
        if entity_kind != 'run-node':
            raise ValueError(f"entity_kind must match const 'run-node', got '{entity_kind}'")

        node_id = d.pop("node_id")

        rank = d.pop("rank")

        reserved_memory_bytes = d.pop("reserved_memory_bytes")

        role = d.pop("role")

        run_id = d.pop("run_id")

        state = d.pop("state")

        def _parse_failure_diagnostics(data: object) -> FailureDiagnostics | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                failure_diagnostics_type_0 = FailureDiagnostics.from_dict(data)



                return failure_diagnostics_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(FailureDiagnostics | None | Unset, data)

        failure_diagnostics = _parse_failure_diagnostics(d.pop("failure_diagnostics", UNSET))


        def _parse_observed_memory_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        observed_memory_bytes = _parse_observed_memory_bytes(d.pop("observed_memory_bytes", UNSET))


        run_node_payload = cls(
            entity_id=entity_id,
            entity_kind=entity_kind,
            node_id=node_id,
            rank=rank,
            reserved_memory_bytes=reserved_memory_bytes,
            role=role,
            run_id=run_id,
            state=state,
            failure_diagnostics=failure_diagnostics,
            observed_memory_bytes=observed_memory_bytes,
        )

        return run_node_payload
