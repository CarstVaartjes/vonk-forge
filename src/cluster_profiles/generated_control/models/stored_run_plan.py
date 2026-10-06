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
  from ..models.stored_run_node_plan import StoredRunNodePlan





T = TypeVar("T", bound="StoredRunPlan")



@_attrs_define
class StoredRunPlan:
    """
        Attributes:
            alias (str):
            installation_id (str):
            mapping_generation (int):
            mapping_id (str):
            nodes (list[StoredRunNodePlan]):
            observation_schema_version (Literal[2]):
            plan_digest (str):
            recipe_revision_id (str):
            run_generation (int):
            schema_version (Literal[1]):
            execution_mode (Literal['one-shot-jobs'] | None | Unset):
     """

    alias: str
    installation_id: str
    mapping_generation: int
    mapping_id: str
    nodes: list[StoredRunNodePlan]
    observation_schema_version: Literal[2]
    plan_digest: str
    recipe_revision_id: str
    run_generation: int
    schema_version: Literal[1]
    execution_mode: Literal['one-shot-jobs'] | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.stored_run_node_plan import StoredRunNodePlan # noqa: PLC0415
        alias = self.alias

        installation_id = self.installation_id

        mapping_generation = self.mapping_generation

        mapping_id = self.mapping_id

        nodes = []
        for nodes_item_data in self.nodes:
            nodes_item = nodes_item_data.to_dict()
            nodes.append(nodes_item)



        observation_schema_version = self.observation_schema_version

        plan_digest = self.plan_digest

        recipe_revision_id = self.recipe_revision_id

        run_generation = self.run_generation

        schema_version = self.schema_version

        execution_mode: Literal['one-shot-jobs'] | None | Unset
        if isinstance(self.execution_mode, Unset):
            execution_mode = UNSET
        else:
            execution_mode = self.execution_mode


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "alias": alias,
            "installation_id": installation_id,
            "mapping_generation": mapping_generation,
            "mapping_id": mapping_id,
            "nodes": nodes,
            "observation_schema_version": observation_schema_version,
            "plan_digest": plan_digest,
            "recipe_revision_id": recipe_revision_id,
            "run_generation": run_generation,
            "schema_version": schema_version,
        })
        if execution_mode is not UNSET:
            field_dict["execution_mode"] = execution_mode

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.stored_run_node_plan import StoredRunNodePlan # noqa: PLC0415
        d = dict(src_dict)
        alias = d.pop("alias")

        installation_id = d.pop("installation_id")

        mapping_generation = d.pop("mapping_generation")

        mapping_id = d.pop("mapping_id")

        nodes = []
        _nodes = d.pop("nodes")
        for nodes_item_data in (_nodes):
            nodes_item = StoredRunNodePlan.from_dict(nodes_item_data)



            nodes.append(nodes_item)


        observation_schema_version = cast(Literal[2] , d.pop("observation_schema_version"))
        if observation_schema_version != 2:
            raise ValueError(f"observation_schema_version must match const 2, got '{observation_schema_version}'")

        plan_digest = d.pop("plan_digest")

        recipe_revision_id = d.pop("recipe_revision_id")

        run_generation = d.pop("run_generation")

        schema_version = cast(Literal[1] , d.pop("schema_version"))
        if schema_version != 1:
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        def _parse_execution_mode(data: object) -> Literal['one-shot-jobs'] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            execution_mode_type_0 = cast(Literal['one-shot-jobs'] , data)
            if execution_mode_type_0 != 'one-shot-jobs':
                raise ValueError(f"execution_mode_type_0 must match const 'one-shot-jobs', got '{execution_mode_type_0}'")
            return execution_mode_type_0
            return cast(Literal['one-shot-jobs'] | None | Unset, data)

        execution_mode = _parse_execution_mode(d.pop("execution_mode", UNSET))


        stored_run_plan = cls(
            alias=alias,
            installation_id=installation_id,
            mapping_generation=mapping_generation,
            mapping_id=mapping_id,
            nodes=nodes,
            observation_schema_version=observation_schema_version,
            plan_digest=plan_digest,
            recipe_revision_id=recipe_revision_id,
            run_generation=run_generation,
            schema_version=schema_version,
            execution_mode=execution_mode,
        )

        return stored_run_plan
