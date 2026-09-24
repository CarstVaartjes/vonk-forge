from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.invalid_operation_evidence_document import check_invalid_operation_evidence_document
from ..models.invalid_operation_evidence_document import InvalidOperationEvidenceDocument
from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="InvalidOperationEvidence")



@_attrs_define
class InvalidOperationEvidence:
    """ A bounded diagnostic for a stored agent receipt outside the current contract.

        Attributes:
            detail (str):
            document (InvalidOperationEvidenceDocument):
            kind (Literal['recipe.start']):
            node_id (str):
            operation_id (str):
     """

    detail: str
    document: InvalidOperationEvidenceDocument
    kind: Literal['recipe.start']
    node_id: str
    operation_id: str





    def to_dict(self) -> dict[str, Any]:
        detail = self.detail

        document: str = self.document

        kind = self.kind

        node_id = self.node_id

        operation_id = self.operation_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "detail": detail,
            "document": document,
            "kind": kind,
            "node_id": node_id,
            "operation_id": operation_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        detail = d.pop("detail")

        document = check_invalid_operation_evidence_document(d.pop("document"))




        kind = cast(Literal['recipe.start'] , d.pop("kind"))
        if kind != 'recipe.start':
            raise ValueError(f"kind must match const 'recipe.start', got '{kind}'")

        node_id = d.pop("node_id")

        operation_id = d.pop("operation_id")

        invalid_operation_evidence = cls(
            detail=detail,
            document=document,
            kind=kind,
            node_id=node_id,
            operation_id=operation_id,
        )

        return invalid_operation_evidence
