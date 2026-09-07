from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.run_switch_final_verify_result_subphase_type_0 import check_run_switch_final_verify_result_subphase_type_0
from ..models.run_switch_final_verify_result_subphase_type_0 import RunSwitchFinalVerifyResultSubphaseType0
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.run_switch_rank_receipt import RunSwitchRankReceipt





T = TypeVar("T", bound="RunSwitchFinalVerifyResult")



@_attrs_define
class RunSwitchFinalVerifyResult:
    """
        Attributes:
            final_verified (bool):
            healthy (bool):
            phase (Literal['final_verify']):
            ranks (list['RunSwitchRankReceipt']):
            route_state (str):
            run_id (str):
            state (str):
            subphase (Union[None, RunSwitchFinalVerifyResultSubphaseType0, Unset]):
     """

    final_verified: bool
    healthy: bool
    phase: Literal['final_verify']
    ranks: list['RunSwitchRankReceipt']
    route_state: str
    run_id: str
    state: str
    subphase: Union[None, RunSwitchFinalVerifyResultSubphaseType0, Unset] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.run_switch_rank_receipt import RunSwitchRankReceipt
        final_verified = self.final_verified

        healthy = self.healthy

        phase = self.phase

        ranks = []
        for ranks_item_data in self.ranks:
            ranks_item = ranks_item_data.to_dict()
            ranks.append(ranks_item)



        route_state = self.route_state

        run_id = self.run_id

        state = self.state

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
            "healthy": healthy,
            "phase": phase,
            "ranks": ranks,
            "route_state": route_state,
            "run_id": run_id,
            "state": state,
        })
        if subphase is not UNSET:
            field_dict["subphase"] = subphase

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.run_switch_rank_receipt import RunSwitchRankReceipt
        d = dict(src_dict)
        final_verified = d.pop("final_verified")

        healthy = d.pop("healthy")

        phase = cast(Literal['final_verify'] , d.pop("phase"))
        if phase != 'final_verify':
            raise ValueError(f"phase must match const 'final_verify', got '{phase}'")

        ranks = []
        _ranks = d.pop("ranks")
        for ranks_item_data in (_ranks):
            ranks_item = RunSwitchRankReceipt.from_dict(ranks_item_data)



            ranks.append(ranks_item)


        route_state = d.pop("route_state")

        run_id = d.pop("run_id")

        state = d.pop("state")

        def _parse_subphase(data: object) -> Union[None, RunSwitchFinalVerifyResultSubphaseType0, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                subphase_type_0 = check_run_switch_final_verify_result_subphase_type_0(data)



                return subphase_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, RunSwitchFinalVerifyResultSubphaseType0, Unset], data)

        subphase = _parse_subphase(d.pop("subphase", UNSET))


        run_switch_final_verify_result = cls(
            final_verified=final_verified,
            healthy=healthy,
            phase=phase,
            ranks=ranks,
            route_state=route_state,
            run_id=run_id,
            state=state,
            subphase=subphase,
        )

        return run_switch_final_verify_result
