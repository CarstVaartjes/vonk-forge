from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_job_activate_parent_owner_kind import check_recipe_job_activate_parent_owner_kind
from ..models.recipe_job_activate_parent_owner_kind import RecipeJobActivateParentOwnerKind
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="RecipeJobActivateParent")



@_attrs_define
class RecipeJobActivateParent:
    """
        Attributes:
            owner_id (str):
            owner_kind (RecipeJobActivateParentOwnerKind):
            plan_digest (str):
            schema_version (Literal[1]):
            execution_mode (Literal['one-shot-jobs'] | None | Unset):
            workload_intent_ordinal (int | None | Unset):
     """

    owner_id: str
    owner_kind: RecipeJobActivateParentOwnerKind
    plan_digest: str
    schema_version: Literal[1]
    execution_mode: Literal['one-shot-jobs'] | None | Unset = UNSET
    workload_intent_ordinal: int | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        owner_id = self.owner_id

        owner_kind: str = self.owner_kind

        plan_digest = self.plan_digest

        schema_version = self.schema_version

        execution_mode: Literal['one-shot-jobs'] | None | Unset
        if isinstance(self.execution_mode, Unset):
            execution_mode = UNSET
        else:
            execution_mode = self.execution_mode

        workload_intent_ordinal: int | None | Unset
        if isinstance(self.workload_intent_ordinal, Unset):
            workload_intent_ordinal = UNSET
        else:
            workload_intent_ordinal = self.workload_intent_ordinal


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "owner_id": owner_id,
            "owner_kind": owner_kind,
            "plan_digest": plan_digest,
            "schema_version": schema_version,
        })
        if execution_mode is not UNSET:
            field_dict["execution_mode"] = execution_mode
        if workload_intent_ordinal is not UNSET:
            field_dict["workload_intent_ordinal"] = workload_intent_ordinal

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        owner_id = d.pop("owner_id")

        owner_kind = check_recipe_job_activate_parent_owner_kind(d.pop("owner_kind"))




        plan_digest = d.pop("plan_digest")

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


        def _parse_workload_intent_ordinal(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        workload_intent_ordinal = _parse_workload_intent_ordinal(d.pop("workload_intent_ordinal", UNSET))


        recipe_job_activate_parent = cls(
            owner_id=owner_id,
            owner_kind=owner_kind,
            plan_digest=plan_digest,
            schema_version=schema_version,
            execution_mode=execution_mode,
            workload_intent_ordinal=workload_intent_ordinal,
        )

        return recipe_job_activate_parent
