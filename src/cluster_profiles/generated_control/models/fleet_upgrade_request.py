from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_upgrade_request_strategy import check_fleet_upgrade_request_strategy
from ..models.fleet_upgrade_request_strategy import FleetUpgradeRequestStrategy
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="FleetUpgradeRequest")



@_attrs_define
class FleetUpgradeRequest:
    """
        Attributes:
            all_ (Union[Unset, bool]):  Default: False.
            selectors (Union[None, Unset, list[str]]):
            strategy (Union[Unset, FleetUpgradeRequestStrategy]):  Default: 'one-at-a-time'.
     """

    all_: Union[Unset, bool] = False
    selectors: Union[None, Unset, list[str]] = UNSET
    strategy: Union[Unset, FleetUpgradeRequestStrategy] = 'one-at-a-time'





    def to_dict(self) -> dict[str, Any]:
        all_ = self.all_

        selectors: Union[None, Unset, list[str]]
        if isinstance(self.selectors, Unset):
            selectors = UNSET
        elif isinstance(self.selectors, list):
            selectors = self.selectors


        else:
            selectors = self.selectors

        strategy: Union[Unset, str] = UNSET
        if not isinstance(self.strategy, Unset):
            strategy = self.strategy



        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if all_ is not UNSET:
            field_dict["all"] = all_
        if selectors is not UNSET:
            field_dict["selectors"] = selectors
        if strategy is not UNSET:
            field_dict["strategy"] = strategy

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        all_ = d.pop("all", UNSET)

        def _parse_selectors(data: object) -> Union[None, Unset, list[str]]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                selectors_type_0 = cast(list[str], data)

                return selectors_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, Unset, list[str]], data)

        selectors = _parse_selectors(d.pop("selectors", UNSET))


        _strategy = d.pop("strategy", UNSET)
        strategy: Union[Unset, FleetUpgradeRequestStrategy]
        if isinstance(_strategy,  Unset):
            strategy = UNSET
        else:
            strategy = check_fleet_upgrade_request_strategy(_strategy)




        fleet_upgrade_request = cls(
            all_=all_,
            selectors=selectors,
            strategy=strategy,
        )

        return fleet_upgrade_request
