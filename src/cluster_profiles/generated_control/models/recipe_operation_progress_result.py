from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.recipe_operation_progress_result_launch_evidence_type_0 import RecipeOperationProgressResultLaunchEvidenceType0
  from ..models.recipe_operation_progress_result_node_evidence_type_0 import RecipeOperationProgressResultNodeEvidenceType0





T = TypeVar("T", bound="RecipeOperationProgressResult")



@_attrs_define
class RecipeOperationProgressResult:
    """ Partial evidence retained while a multi-node operation is running.

        Attributes:
            launch_evidence (None | RecipeOperationProgressResultLaunchEvidenceType0 | Unset):
            node_evidence (None | RecipeOperationProgressResultNodeEvidenceType0 | Unset):
     """

    launch_evidence: None | RecipeOperationProgressResultLaunchEvidenceType0 | Unset = UNSET
    node_evidence: None | RecipeOperationProgressResultNodeEvidenceType0 | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_operation_progress_result_launch_evidence_type_0 import RecipeOperationProgressResultLaunchEvidenceType0 # noqa: PLC0415
        from ..models.recipe_operation_progress_result_node_evidence_type_0 import RecipeOperationProgressResultNodeEvidenceType0 # noqa: PLC0415
        launch_evidence: dict[str, Any] | None | Unset
        if isinstance(self.launch_evidence, Unset):
            launch_evidence = UNSET
        elif isinstance(self.launch_evidence, RecipeOperationProgressResultLaunchEvidenceType0):
            launch_evidence = self.launch_evidence.to_dict()
        else:
            launch_evidence = self.launch_evidence

        node_evidence: dict[str, Any] | None | Unset
        if isinstance(self.node_evidence, Unset):
            node_evidence = UNSET
        elif isinstance(self.node_evidence, RecipeOperationProgressResultNodeEvidenceType0):
            node_evidence = self.node_evidence.to_dict()
        else:
            node_evidence = self.node_evidence


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if launch_evidence is not UNSET:
            field_dict["launch_evidence"] = launch_evidence
        if node_evidence is not UNSET:
            field_dict["node_evidence"] = node_evidence

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_operation_progress_result_launch_evidence_type_0 import RecipeOperationProgressResultLaunchEvidenceType0 # noqa: PLC0415
        from ..models.recipe_operation_progress_result_node_evidence_type_0 import RecipeOperationProgressResultNodeEvidenceType0 # noqa: PLC0415
        d = dict(src_dict)
        def _parse_launch_evidence(data: object) -> None | RecipeOperationProgressResultLaunchEvidenceType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                launch_evidence_type_0 = RecipeOperationProgressResultLaunchEvidenceType0.from_dict(data)



                return launch_evidence_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RecipeOperationProgressResultLaunchEvidenceType0 | Unset, data)

        launch_evidence = _parse_launch_evidence(d.pop("launch_evidence", UNSET))


        def _parse_node_evidence(data: object) -> None | RecipeOperationProgressResultNodeEvidenceType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                node_evidence_type_0 = RecipeOperationProgressResultNodeEvidenceType0.from_dict(data)



                return node_evidence_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RecipeOperationProgressResultNodeEvidenceType0 | Unset, data)

        node_evidence = _parse_node_evidence(d.pop("node_evidence", UNSET))


        recipe_operation_progress_result = cls(
            launch_evidence=launch_evidence,
            node_evidence=node_evidence,
        )

        return recipe_operation_progress_result
