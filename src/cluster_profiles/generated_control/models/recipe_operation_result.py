from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.recipe_operation_result_launch_evidence_type_0 import RecipeOperationResultLaunchEvidenceType0
  from ..models.recipe_operation_result_node_evidence import RecipeOperationResultNodeEvidence





T = TypeVar("T", bound="RecipeOperationResult")



@_attrs_define
class RecipeOperationResult:
    """ Terminal aggregate emitted by the durable lifecycle job projector.

        Attributes:
            failed_nodes (list[str]):
            node_evidence (RecipeOperationResultNodeEvidence):
            successful_nodes (list[str]):
            launch_evidence (None | RecipeOperationResultLaunchEvidenceType0 | Unset):
            recovery_error (None | str | Unset):
     """

    failed_nodes: list[str]
    node_evidence: RecipeOperationResultNodeEvidence
    successful_nodes: list[str]
    launch_evidence: None | RecipeOperationResultLaunchEvidenceType0 | Unset = UNSET
    recovery_error: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_operation_result_launch_evidence_type_0 import RecipeOperationResultLaunchEvidenceType0 # noqa: PLC0415
        from ..models.recipe_operation_result_node_evidence import RecipeOperationResultNodeEvidence # noqa: PLC0415
        failed_nodes = self.failed_nodes



        node_evidence = self.node_evidence.to_dict()

        successful_nodes = self.successful_nodes



        launch_evidence: dict[str, Any] | None | Unset
        if isinstance(self.launch_evidence, Unset):
            launch_evidence = UNSET
        elif isinstance(self.launch_evidence, RecipeOperationResultLaunchEvidenceType0):
            launch_evidence = self.launch_evidence.to_dict()
        else:
            launch_evidence = self.launch_evidence

        recovery_error: None | str | Unset
        if isinstance(self.recovery_error, Unset):
            recovery_error = UNSET
        else:
            recovery_error = self.recovery_error


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "failed_nodes": failed_nodes,
            "node_evidence": node_evidence,
            "successful_nodes": successful_nodes,
        })
        if launch_evidence is not UNSET:
            field_dict["launch_evidence"] = launch_evidence
        if recovery_error is not UNSET:
            field_dict["recovery_error"] = recovery_error

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_operation_result_launch_evidence_type_0 import RecipeOperationResultLaunchEvidenceType0 # noqa: PLC0415
        from ..models.recipe_operation_result_node_evidence import RecipeOperationResultNodeEvidence # noqa: PLC0415
        d = dict(src_dict)
        failed_nodes = cast(list[str], d.pop("failed_nodes"))


        node_evidence = RecipeOperationResultNodeEvidence.from_dict(d.pop("node_evidence"))




        successful_nodes = cast(list[str], d.pop("successful_nodes"))


        def _parse_launch_evidence(data: object) -> None | RecipeOperationResultLaunchEvidenceType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                launch_evidence_type_0 = RecipeOperationResultLaunchEvidenceType0.from_dict(data)



                return launch_evidence_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RecipeOperationResultLaunchEvidenceType0 | Unset, data)

        launch_evidence = _parse_launch_evidence(d.pop("launch_evidence", UNSET))


        def _parse_recovery_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        recovery_error = _parse_recovery_error(d.pop("recovery_error", UNSET))


        recipe_operation_result = cls(
            failed_nodes=failed_nodes,
            node_evidence=node_evidence,
            successful_nodes=successful_nodes,
            launch_evidence=launch_evidence,
            recovery_error=recovery_error,
        )

        return recipe_operation_result
