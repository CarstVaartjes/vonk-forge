from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.run_switch_phase_kind import check_run_switch_phase_kind
from ..models.run_switch_phase_kind import RunSwitchPhaseKind
from ..models.run_switch_phase_state import check_run_switch_phase_state
from ..models.run_switch_phase_state import RunSwitchPhaseState
from ..models.run_switch_phase_subphase_type_0 import check_run_switch_phase_subphase_type_0
from ..models.run_switch_phase_subphase_type_0 import RunSwitchPhaseSubphaseType0
from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="RunSwitchPhase")



@_attrs_define
class RunSwitchPhase:
    """
        Attributes:
            detail (str):
            index (int):
            kind (RunSwitchPhaseKind):
            state (RunSwitchPhaseState):
            node_ids (list[str] | Unset):
            operation_digest (None | str | Unset):
            subphase (None | RunSwitchPhaseSubphaseType0 | Unset):
     """

    detail: str
    index: int
    kind: RunSwitchPhaseKind
    state: RunSwitchPhaseState
    node_ids: list[str] | Unset = UNSET
    operation_digest: None | str | Unset = UNSET
    subphase: None | RunSwitchPhaseSubphaseType0 | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        detail = self.detail

        index = self.index

        kind: str = self.kind

        state: str = self.state

        node_ids: list[str] | Unset = UNSET
        if not isinstance(self.node_ids, Unset):
            node_ids = self.node_ids



        operation_digest: None | str | Unset
        if isinstance(self.operation_digest, Unset):
            operation_digest = UNSET
        else:
            operation_digest = self.operation_digest

        subphase: None | str | Unset
        if isinstance(self.subphase, Unset):
            subphase = UNSET
        elif isinstance(self.subphase, str):
            subphase = self.subphase
        else:
            subphase = self.subphase


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "detail": detail,
            "index": index,
            "kind": kind,
            "state": state,
        })
        if node_ids is not UNSET:
            field_dict["node_ids"] = node_ids
        if operation_digest is not UNSET:
            field_dict["operation_digest"] = operation_digest
        if subphase is not UNSET:
            field_dict["subphase"] = subphase

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        detail = d.pop("detail")

        index = d.pop("index")

        kind = check_run_switch_phase_kind(d.pop("kind"))




        state = check_run_switch_phase_state(d.pop("state"))




        node_ids = cast(list[str], d.pop("node_ids", UNSET))


        def _parse_operation_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        operation_digest = _parse_operation_digest(d.pop("operation_digest", UNSET))


        def _parse_subphase(data: object) -> None | RunSwitchPhaseSubphaseType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                subphase_type_0 = check_run_switch_phase_subphase_type_0(data)



                return subphase_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RunSwitchPhaseSubphaseType0 | Unset, data)

        subphase = _parse_subphase(d.pop("subphase", UNSET))


        run_switch_phase = cls(
            detail=detail,
            index=index,
            kind=kind,
            state=state,
            node_ids=node_ids,
            operation_digest=operation_digest,
            subphase=subphase,
        )

        return run_switch_phase
