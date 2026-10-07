from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="ProfileStopOwnerBinding")



@_attrs_define
class ProfileStopOwnerBinding:
    """ The canonical accepted profile operation that owns this exact Stop.

        Attributes:
            installation_id (str):
            mapping_generation (int):
            mapping_id (str):
            plan_digest (str):
            profile_application_id (str):
            profile_digest (str):
            profile_operation_id (str):
            profile_plan_digest (str):
            profile_step (int):
            reachable_node_ids (list[str]):
            recipe_revision_id (str):
            run_generation (int):
            run_id (str):
            run_node_ids (list[str]):
            schema_version (Literal[1]):
            stop_plan_digest (str):
            workload_intent_ordinal (int):
            missing_node_ids (list[str] | Unset):
     """

    installation_id: str
    mapping_generation: int
    mapping_id: str
    plan_digest: str
    profile_application_id: str
    profile_digest: str
    profile_operation_id: str
    profile_plan_digest: str
    profile_step: int
    reachable_node_ids: list[str]
    recipe_revision_id: str
    run_generation: int
    run_id: str
    run_node_ids: list[str]
    schema_version: Literal[1]
    stop_plan_digest: str
    workload_intent_ordinal: int
    missing_node_ids: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        installation_id = self.installation_id

        mapping_generation = self.mapping_generation

        mapping_id = self.mapping_id

        plan_digest = self.plan_digest

        profile_application_id = self.profile_application_id

        profile_digest = self.profile_digest

        profile_operation_id = self.profile_operation_id

        profile_plan_digest = self.profile_plan_digest

        profile_step = self.profile_step

        reachable_node_ids = self.reachable_node_ids



        recipe_revision_id = self.recipe_revision_id

        run_generation = self.run_generation

        run_id = self.run_id

        run_node_ids = self.run_node_ids



        schema_version = self.schema_version

        stop_plan_digest = self.stop_plan_digest

        workload_intent_ordinal = self.workload_intent_ordinal

        missing_node_ids: list[str] | Unset = UNSET
        if not isinstance(self.missing_node_ids, Unset):
            missing_node_ids = self.missing_node_ids




        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "installation_id": installation_id,
            "mapping_generation": mapping_generation,
            "mapping_id": mapping_id,
            "plan_digest": plan_digest,
            "profile_application_id": profile_application_id,
            "profile_digest": profile_digest,
            "profile_operation_id": profile_operation_id,
            "profile_plan_digest": profile_plan_digest,
            "profile_step": profile_step,
            "reachable_node_ids": reachable_node_ids,
            "recipe_revision_id": recipe_revision_id,
            "run_generation": run_generation,
            "run_id": run_id,
            "run_node_ids": run_node_ids,
            "schema_version": schema_version,
            "stop_plan_digest": stop_plan_digest,
            "workload_intent_ordinal": workload_intent_ordinal,
        })
        if missing_node_ids is not UNSET:
            field_dict["missing_node_ids"] = missing_node_ids

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        installation_id = d.pop("installation_id")

        mapping_generation = d.pop("mapping_generation")

        mapping_id = d.pop("mapping_id")

        plan_digest = d.pop("plan_digest")

        profile_application_id = d.pop("profile_application_id")

        profile_digest = d.pop("profile_digest")

        profile_operation_id = d.pop("profile_operation_id")

        profile_plan_digest = d.pop("profile_plan_digest")

        profile_step = d.pop("profile_step")

        reachable_node_ids = cast(list[str], d.pop("reachable_node_ids"))


        recipe_revision_id = d.pop("recipe_revision_id")

        run_generation = d.pop("run_generation")

        run_id = d.pop("run_id")

        run_node_ids = cast(list[str], d.pop("run_node_ids"))


        schema_version = cast(Literal[1] , d.pop("schema_version"))
        if schema_version != 1:
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        stop_plan_digest = d.pop("stop_plan_digest")

        workload_intent_ordinal = d.pop("workload_intent_ordinal")

        missing_node_ids = cast(list[str], d.pop("missing_node_ids", UNSET))


        profile_stop_owner_binding = cls(
            installation_id=installation_id,
            mapping_generation=mapping_generation,
            mapping_id=mapping_id,
            plan_digest=plan_digest,
            profile_application_id=profile_application_id,
            profile_digest=profile_digest,
            profile_operation_id=profile_operation_id,
            profile_plan_digest=profile_plan_digest,
            profile_step=profile_step,
            reachable_node_ids=reachable_node_ids,
            recipe_revision_id=recipe_revision_id,
            run_generation=run_generation,
            run_id=run_id,
            run_node_ids=run_node_ids,
            schema_version=schema_version,
            stop_plan_digest=stop_plan_digest,
            workload_intent_ordinal=workload_intent_ordinal,
            missing_node_ids=missing_node_ids,
        )


        profile_stop_owner_binding.additional_properties = d
        return profile_stop_owner_binding

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
