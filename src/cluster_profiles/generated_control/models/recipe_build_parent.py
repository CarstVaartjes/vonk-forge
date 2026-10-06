from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_build_parent_owner_kind import check_recipe_build_parent_owner_kind
from ..models.recipe_build_parent_owner_kind import RecipeBuildParentOwnerKind
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.build_phase_operation import BuildPhaseOperation
  from ..models.recipe_build_intent import RecipeBuildIntent





T = TypeVar("T", bound="RecipeBuildParent")



@_attrs_define
class RecipeBuildParent:
    """
        Attributes:
            owner_id (str):
            owner_kind (RecipeBuildParentOwnerKind):
            plan_digest (str):
            schema_version (Literal[1]):
            build_intent (None | RecipeBuildIntent | Unset):
            execution_mode (Literal['one-shot-jobs'] | None | Unset):
            force_rebuild (bool | None | Unset):
            phases (list[list[BuildPhaseOperation]] | None | Unset):
            prebuilt_image (None | str | Unset):
            prebuilt_node_id (None | str | Unset):
            workload_intent_ordinal (int | None | Unset):
     """

    owner_id: str
    owner_kind: RecipeBuildParentOwnerKind
    plan_digest: str
    schema_version: Literal[1]
    build_intent: None | RecipeBuildIntent | Unset = UNSET
    execution_mode: Literal['one-shot-jobs'] | None | Unset = UNSET
    force_rebuild: bool | None | Unset = UNSET
    phases: list[list[BuildPhaseOperation]] | None | Unset = UNSET
    prebuilt_image: None | str | Unset = UNSET
    prebuilt_node_id: None | str | Unset = UNSET
    workload_intent_ordinal: int | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.build_phase_operation import BuildPhaseOperation # noqa: PLC0415
        from ..models.recipe_build_intent import RecipeBuildIntent # noqa: PLC0415
        owner_id = self.owner_id

        owner_kind: str = self.owner_kind

        plan_digest = self.plan_digest

        schema_version = self.schema_version

        build_intent: dict[str, Any] | None | Unset
        if isinstance(self.build_intent, Unset):
            build_intent = UNSET
        elif isinstance(self.build_intent, RecipeBuildIntent):
            build_intent = self.build_intent.to_dict()
        else:
            build_intent = self.build_intent

        execution_mode: Literal['one-shot-jobs'] | None | Unset
        if isinstance(self.execution_mode, Unset):
            execution_mode = UNSET
        else:
            execution_mode = self.execution_mode

        force_rebuild: bool | None | Unset
        if isinstance(self.force_rebuild, Unset):
            force_rebuild = UNSET
        else:
            force_rebuild = self.force_rebuild

        phases: list[list[dict[str, Any]]] | None | Unset
        if isinstance(self.phases, Unset):
            phases = UNSET
        elif isinstance(self.phases, list):
            phases = []
            for phases_type_0_item_data in self.phases:
                phases_type_0_item = []
                for phases_type_0_item_item_data in phases_type_0_item_data:
                    phases_type_0_item_item = phases_type_0_item_item_data.to_dict()
                    phases_type_0_item.append(phases_type_0_item_item)


                phases.append(phases_type_0_item)


        else:
            phases = self.phases

        prebuilt_image: None | str | Unset
        if isinstance(self.prebuilt_image, Unset):
            prebuilt_image = UNSET
        else:
            prebuilt_image = self.prebuilt_image

        prebuilt_node_id: None | str | Unset
        if isinstance(self.prebuilt_node_id, Unset):
            prebuilt_node_id = UNSET
        else:
            prebuilt_node_id = self.prebuilt_node_id

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
        if build_intent is not UNSET:
            field_dict["build_intent"] = build_intent
        if execution_mode is not UNSET:
            field_dict["execution_mode"] = execution_mode
        if force_rebuild is not UNSET:
            field_dict["force_rebuild"] = force_rebuild
        if phases is not UNSET:
            field_dict["phases"] = phases
        if prebuilt_image is not UNSET:
            field_dict["prebuilt_image"] = prebuilt_image
        if prebuilt_node_id is not UNSET:
            field_dict["prebuilt_node_id"] = prebuilt_node_id
        if workload_intent_ordinal is not UNSET:
            field_dict["workload_intent_ordinal"] = workload_intent_ordinal

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.build_phase_operation import BuildPhaseOperation # noqa: PLC0415
        from ..models.recipe_build_intent import RecipeBuildIntent # noqa: PLC0415
        d = dict(src_dict)
        owner_id = d.pop("owner_id")

        owner_kind = check_recipe_build_parent_owner_kind(d.pop("owner_kind"))




        plan_digest = d.pop("plan_digest")

        schema_version = cast(Literal[1] , d.pop("schema_version"))
        if schema_version != 1:
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        def _parse_build_intent(data: object) -> None | RecipeBuildIntent | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                build_intent_type_0 = RecipeBuildIntent.from_dict(data)



                return build_intent_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RecipeBuildIntent | Unset, data)

        build_intent = _parse_build_intent(d.pop("build_intent", UNSET))


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


        def _parse_force_rebuild(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        force_rebuild = _parse_force_rebuild(d.pop("force_rebuild", UNSET))


        def _parse_phases(data: object) -> list[list[BuildPhaseOperation]] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                phases_type_0 = []
                _phases_type_0 = data
                for phases_type_0_item_data in (_phases_type_0):
                    phases_type_0_item = []
                    _phases_type_0_item = phases_type_0_item_data
                    for phases_type_0_item_item_data in (_phases_type_0_item):
                        phases_type_0_item_item = BuildPhaseOperation.from_dict(phases_type_0_item_item_data)



                        phases_type_0_item.append(phases_type_0_item_item)

                    phases_type_0.append(phases_type_0_item)

                return phases_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[list[BuildPhaseOperation]] | None | Unset, data)

        phases = _parse_phases(d.pop("phases", UNSET))


        def _parse_prebuilt_image(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        prebuilt_image = _parse_prebuilt_image(d.pop("prebuilt_image", UNSET))


        def _parse_prebuilt_node_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        prebuilt_node_id = _parse_prebuilt_node_id(d.pop("prebuilt_node_id", UNSET))


        def _parse_workload_intent_ordinal(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        workload_intent_ordinal = _parse_workload_intent_ordinal(d.pop("workload_intent_ordinal", UNSET))


        recipe_build_parent = cls(
            owner_id=owner_id,
            owner_kind=owner_kind,
            plan_digest=plan_digest,
            schema_version=schema_version,
            build_intent=build_intent,
            execution_mode=execution_mode,
            force_rebuild=force_rebuild,
            phases=phases,
            prebuilt_image=prebuilt_image,
            prebuilt_node_id=prebuilt_node_id,
            workload_intent_ordinal=workload_intent_ordinal,
        )

        return recipe_build_parent
