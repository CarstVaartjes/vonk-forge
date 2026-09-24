from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.run_switch_installation_verify_result_subphase_type_0 import check_run_switch_installation_verify_result_subphase_type_0
from ..models.run_switch_installation_verify_result_subphase_type_0 import RunSwitchInstallationVerifyResultSubphaseType0
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.run_switch_rank_receipt import RunSwitchRankReceipt





T = TypeVar("T", bound="RunSwitchInstallationVerifyResult")



@_attrs_define
class RunSwitchInstallationVerifyResult:
    """ Exact installed membership observed without a serving workload.

        Attributes:
            active_runs (int):
            final_verified (bool):
            installation_id (str):
            installation_state (str):
            phase (Literal['final_verify']):
            ranks (list['RunSwitchRankReceipt']):
            unwithdrawn_routes (int):
            subphase (Union[None, RunSwitchInstallationVerifyResultSubphaseType0, Unset]):
     """

    active_runs: int
    final_verified: bool
    installation_id: str
    installation_state: str
    phase: Literal['final_verify']
    ranks: list['RunSwitchRankReceipt']
    unwithdrawn_routes: int
    subphase: Union[None, RunSwitchInstallationVerifyResultSubphaseType0, Unset] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.run_switch_rank_receipt import RunSwitchRankReceipt
        active_runs = self.active_runs

        final_verified = self.final_verified

        installation_id = self.installation_id

        installation_state = self.installation_state

        phase = self.phase

        ranks = []
        for ranks_item_data in self.ranks:
            ranks_item = ranks_item_data.to_dict()
            ranks.append(ranks_item)



        unwithdrawn_routes = self.unwithdrawn_routes

        subphase: Union[None, Unset, str]
        if isinstance(self.subphase, Unset):
            subphase = UNSET
        elif isinstance(self.subphase, str):
            subphase = self.subphase
        else:
            subphase = self.subphase


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "active_runs": active_runs,
            "final_verified": final_verified,
            "installation_id": installation_id,
            "installation_state": installation_state,
            "phase": phase,
            "ranks": ranks,
            "unwithdrawn_routes": unwithdrawn_routes,
        })
        if subphase is not UNSET:
            field_dict["subphase"] = subphase

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.run_switch_rank_receipt import RunSwitchRankReceipt
        d = dict(src_dict)
        active_runs = d.pop("active_runs")

        final_verified = d.pop("final_verified")

        installation_id = d.pop("installation_id")

        installation_state = d.pop("installation_state")

        phase = cast(Literal['final_verify'] , d.pop("phase"))
        if phase != 'final_verify':
            raise ValueError(f"phase must match const 'final_verify', got '{phase}'")

        ranks = []
        _ranks = d.pop("ranks")
        for ranks_item_data in (_ranks):
            ranks_item = RunSwitchRankReceipt.from_dict(ranks_item_data)



            ranks.append(ranks_item)


        unwithdrawn_routes = d.pop("unwithdrawn_routes")

        def _parse_subphase(data: object) -> Union[None, RunSwitchInstallationVerifyResultSubphaseType0, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                subphase_type_0 = check_run_switch_installation_verify_result_subphase_type_0(data)



                return subphase_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, RunSwitchInstallationVerifyResultSubphaseType0, Unset], data)

        subphase = _parse_subphase(d.pop("subphase", UNSET))


        run_switch_installation_verify_result = cls(
            active_runs=active_runs,
            final_verified=final_verified,
            installation_id=installation_id,
            installation_state=installation_state,
            phase=phase,
            ranks=ranks,
            unwithdrawn_routes=unwithdrawn_routes,
            subphase=subphase,
        )

        return run_switch_installation_verify_result
