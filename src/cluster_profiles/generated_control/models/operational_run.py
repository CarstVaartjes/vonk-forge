from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.operational_run_route_state import check_operational_run_route_state
from ..models.operational_run_route_state import OperationalRunRouteState
from ..models.operational_run_state import check_operational_run_state
from ..models.operational_run_state import OperationalRunState
from typing import cast






T = TypeVar("T", bound="OperationalRun")



@_attrs_define
class OperationalRun:
    """
        Attributes:
            installation_id (str):
            mapping_id (str):
            node_ids (list[str]):
            recipe_revision_id (str):
            route_state (OperationalRunRouteState):
            run_id (str):
            state (OperationalRunState):
     """

    installation_id: str
    mapping_id: str
    node_ids: list[str]
    recipe_revision_id: str
    route_state: OperationalRunRouteState
    run_id: str
    state: OperationalRunState





    def to_dict(self) -> dict[str, Any]:
        installation_id = self.installation_id

        mapping_id = self.mapping_id

        node_ids = self.node_ids



        recipe_revision_id = self.recipe_revision_id

        route_state: str = self.route_state

        run_id = self.run_id

        state: str = self.state


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "installation_id": installation_id,
            "mapping_id": mapping_id,
            "node_ids": node_ids,
            "recipe_revision_id": recipe_revision_id,
            "route_state": route_state,
            "run_id": run_id,
            "state": state,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        installation_id = d.pop("installation_id")

        mapping_id = d.pop("mapping_id")

        node_ids = cast(list[str], d.pop("node_ids"))


        recipe_revision_id = d.pop("recipe_revision_id")

        route_state = check_operational_run_route_state(d.pop("route_state"))




        run_id = d.pop("run_id")

        state = check_operational_run_state(d.pop("state"))




        operational_run = cls(
            installation_id=installation_id,
            mapping_id=mapping_id,
            node_ids=node_ids,
            recipe_revision_id=recipe_revision_id,
            route_state=route_state,
            run_id=run_id,
            state=state,
        )

        return operational_run
