from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.run_switch_cleanup_verify_result_cleanup_mode import check_run_switch_cleanup_verify_result_cleanup_mode
from ..models.run_switch_cleanup_verify_result_cleanup_mode import RunSwitchCleanupVerifyResultCleanupMode
from ..models.run_switch_cleanup_verify_result_subphase_type_0 import check_run_switch_cleanup_verify_result_subphase_type_0
from ..models.run_switch_cleanup_verify_result_subphase_type_0 import RunSwitchCleanupVerifyResultSubphaseType0
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.recipe_reconcile_result import RecipeReconcileResult





T = TypeVar("T", bound="RunSwitchCleanupVerifyResult")



@_attrs_define
class RunSwitchCleanupVerifyResult:
    """ Observed removal of the installation, derived from durable state.

        Attributes:
            final_verified (bool):
            installation_id (str):
            phase (Literal['final_verify']):
            removed (bool):
            active_runs (Union[Unset, int]):  Default: 0.
            cleanup_mode (Union[Unset, RunSwitchCleanupVerifyResultCleanupMode]):  Default: 'uninstall'.
            exact_reconciliation_receipts (Union[None, Unset, bool]):
            installation_state (Union[None, Unset, str]):
            reconciliation_receipts (Union[Unset, list['RecipeReconcileResult']]):
            reconciliation_request_id (Union[None, Unset, str]):
            subphase (Union[None, RunSwitchCleanupVerifyResultSubphaseType0, Unset]):
     """

    final_verified: bool
    installation_id: str
    phase: Literal['final_verify']
    removed: bool
    active_runs: Union[Unset, int] = 0
    cleanup_mode: Union[Unset, RunSwitchCleanupVerifyResultCleanupMode] = 'uninstall'
    exact_reconciliation_receipts: Union[None, Unset, bool] = UNSET
    installation_state: Union[None, Unset, str] = UNSET
    reconciliation_receipts: Union[Unset, list['RecipeReconcileResult']] = UNSET
    reconciliation_request_id: Union[None, Unset, str] = UNSET
    subphase: Union[None, RunSwitchCleanupVerifyResultSubphaseType0, Unset] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_reconcile_result import RecipeReconcileResult
        final_verified = self.final_verified

        installation_id = self.installation_id

        phase = self.phase

        removed = self.removed

        active_runs = self.active_runs

        cleanup_mode: Union[Unset, str] = UNSET
        if not isinstance(self.cleanup_mode, Unset):
            cleanup_mode = self.cleanup_mode


        exact_reconciliation_receipts: Union[None, Unset, bool]
        if isinstance(self.exact_reconciliation_receipts, Unset):
            exact_reconciliation_receipts = UNSET
        else:
            exact_reconciliation_receipts = self.exact_reconciliation_receipts

        installation_state: Union[None, Unset, str]
        if isinstance(self.installation_state, Unset):
            installation_state = UNSET
        else:
            installation_state = self.installation_state

        reconciliation_receipts: Union[Unset, list[dict[str, Any]]] = UNSET
        if not isinstance(self.reconciliation_receipts, Unset):
            reconciliation_receipts = []
            for reconciliation_receipts_item_data in self.reconciliation_receipts:
                reconciliation_receipts_item = reconciliation_receipts_item_data.to_dict()
                reconciliation_receipts.append(reconciliation_receipts_item)



        reconciliation_request_id: Union[None, Unset, str]
        if isinstance(self.reconciliation_request_id, Unset):
            reconciliation_request_id = UNSET
        else:
            reconciliation_request_id = self.reconciliation_request_id

        subphase: Union[None, Unset, str]
        if isinstance(self.subphase, Unset):
            subphase = UNSET
        elif isinstance(self.subphase, str):
            subphase = self.subphase
        else:
            subphase = self.subphase


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "final_verified": final_verified,
            "installation_id": installation_id,
            "phase": phase,
            "removed": removed,
        })
        if active_runs is not UNSET:
            field_dict["active_runs"] = active_runs
        if cleanup_mode is not UNSET:
            field_dict["cleanup_mode"] = cleanup_mode
        if exact_reconciliation_receipts is not UNSET:
            field_dict["exact_reconciliation_receipts"] = exact_reconciliation_receipts
        if installation_state is not UNSET:
            field_dict["installation_state"] = installation_state
        if reconciliation_receipts is not UNSET:
            field_dict["reconciliation_receipts"] = reconciliation_receipts
        if reconciliation_request_id is not UNSET:
            field_dict["reconciliation_request_id"] = reconciliation_request_id
        if subphase is not UNSET:
            field_dict["subphase"] = subphase

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_reconcile_result import RecipeReconcileResult
        d = dict(src_dict)
        final_verified = d.pop("final_verified")

        installation_id = d.pop("installation_id")

        phase = cast(Literal['final_verify'] , d.pop("phase"))
        if phase != 'final_verify':
            raise ValueError(f"phase must match const 'final_verify', got '{phase}'")

        removed = d.pop("removed")

        active_runs = d.pop("active_runs", UNSET)

        _cleanup_mode = d.pop("cleanup_mode", UNSET)
        cleanup_mode: Union[Unset, RunSwitchCleanupVerifyResultCleanupMode]
        if isinstance(_cleanup_mode,  Unset):
            cleanup_mode = UNSET
        else:
            cleanup_mode = check_run_switch_cleanup_verify_result_cleanup_mode(_cleanup_mode)




        def _parse_exact_reconciliation_receipts(data: object) -> Union[None, Unset, bool]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, bool], data)

        exact_reconciliation_receipts = _parse_exact_reconciliation_receipts(d.pop("exact_reconciliation_receipts", UNSET))


        def _parse_installation_state(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        installation_state = _parse_installation_state(d.pop("installation_state", UNSET))


        reconciliation_receipts = []
        _reconciliation_receipts = d.pop("reconciliation_receipts", UNSET)
        for reconciliation_receipts_item_data in (_reconciliation_receipts or []):
            reconciliation_receipts_item = RecipeReconcileResult.from_dict(reconciliation_receipts_item_data)



            reconciliation_receipts.append(reconciliation_receipts_item)


        def _parse_reconciliation_request_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        reconciliation_request_id = _parse_reconciliation_request_id(d.pop("reconciliation_request_id", UNSET))


        def _parse_subphase(data: object) -> Union[None, RunSwitchCleanupVerifyResultSubphaseType0, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                subphase_type_0 = check_run_switch_cleanup_verify_result_subphase_type_0(data)



                return subphase_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, RunSwitchCleanupVerifyResultSubphaseType0, Unset], data)

        subphase = _parse_subphase(d.pop("subphase", UNSET))


        run_switch_cleanup_verify_result = cls(
            final_verified=final_verified,
            installation_id=installation_id,
            phase=phase,
            removed=removed,
            active_runs=active_runs,
            cleanup_mode=cleanup_mode,
            exact_reconciliation_receipts=exact_reconciliation_receipts,
            installation_state=installation_state,
            reconciliation_receipts=reconciliation_receipts,
            reconciliation_request_id=reconciliation_request_id,
            subphase=subphase,
        )

        return run_switch_cleanup_verify_result
