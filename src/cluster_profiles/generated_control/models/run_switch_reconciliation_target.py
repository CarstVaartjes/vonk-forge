from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.run_switch_reconciliation_target_state import check_run_switch_reconciliation_target_state
from ..models.run_switch_reconciliation_target_state import RunSwitchReconciliationTargetState
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="RunSwitchReconciliationTarget")



@_attrs_define
class RunSwitchReconciliationTarget:
    """ One exact rank and its current cleanup receipt state.

        Attributes:
            compiled_spec_canonical_sha256 (str):
            install_operation_id (str):
            install_operation_payload_sha256 (str):
            installed_bytes (int):
            node_id (str):
            rank (int):
            role (str):
            state (RunSwitchReconciliationTargetState):
            cleanup_receipt_sha256 (Union[None, Unset, str]):
     """

    compiled_spec_canonical_sha256: str
    install_operation_id: str
    install_operation_payload_sha256: str
    installed_bytes: int
    node_id: str
    rank: int
    role: str
    state: RunSwitchReconciliationTargetState
    cleanup_receipt_sha256: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        compiled_spec_canonical_sha256 = self.compiled_spec_canonical_sha256

        install_operation_id = self.install_operation_id

        install_operation_payload_sha256 = self.install_operation_payload_sha256

        installed_bytes = self.installed_bytes

        node_id = self.node_id

        rank = self.rank

        role = self.role

        state: str = self.state

        cleanup_receipt_sha256: Union[None, Unset, str]
        if isinstance(self.cleanup_receipt_sha256, Unset):
            cleanup_receipt_sha256 = UNSET
        else:
            cleanup_receipt_sha256 = self.cleanup_receipt_sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "compiled_spec_canonical_sha256": compiled_spec_canonical_sha256,
            "install_operation_id": install_operation_id,
            "install_operation_payload_sha256": install_operation_payload_sha256,
            "installed_bytes": installed_bytes,
            "node_id": node_id,
            "rank": rank,
            "role": role,
            "state": state,
        })
        if cleanup_receipt_sha256 is not UNSET:
            field_dict["cleanup_receipt_sha256"] = cleanup_receipt_sha256

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        compiled_spec_canonical_sha256 = d.pop("compiled_spec_canonical_sha256")

        install_operation_id = d.pop("install_operation_id")

        install_operation_payload_sha256 = d.pop("install_operation_payload_sha256")

        installed_bytes = d.pop("installed_bytes")

        node_id = d.pop("node_id")

        rank = d.pop("rank")

        role = d.pop("role")

        state = check_run_switch_reconciliation_target_state(d.pop("state"))




        def _parse_cleanup_receipt_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        cleanup_receipt_sha256 = _parse_cleanup_receipt_sha256(d.pop("cleanup_receipt_sha256", UNSET))


        run_switch_reconciliation_target = cls(
            compiled_spec_canonical_sha256=compiled_spec_canonical_sha256,
            install_operation_id=install_operation_id,
            install_operation_payload_sha256=install_operation_payload_sha256,
            installed_bytes=installed_bytes,
            node_id=node_id,
            rank=rank,
            role=role,
            state=state,
            cleanup_receipt_sha256=cleanup_receipt_sha256,
        )

        return run_switch_reconciliation_target
