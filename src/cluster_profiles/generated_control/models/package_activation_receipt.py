from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.package_activation_receipt_phase import check_package_activation_receipt_phase
from ..models.package_activation_receipt_phase import PackageActivationReceiptPhase
from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="PackageActivationReceipt")



@_attrs_define
class PackageActivationReceipt:
    """
        Attributes:
            attempt_nonce (str):
            candidate_binary_sha256 (str):
            candidate_package_sha256 (str):
            candidate_version (str):
            created_at (int):
            node_id (str):
            outcome (str):
            phase (PackageActivationReceiptPhase):
            schema_version (Literal[2]):
            source_binary_sha256 (str):
            source_package_sha256 (str):
            source_version (str):
            updated_at (int):
     """

    attempt_nonce: str
    candidate_binary_sha256: str
    candidate_package_sha256: str
    candidate_version: str
    created_at: int
    node_id: str
    outcome: str
    phase: PackageActivationReceiptPhase
    schema_version: Literal[2]
    source_binary_sha256: str
    source_package_sha256: str
    source_version: str
    updated_at: int





    def to_dict(self) -> dict[str, Any]:
        attempt_nonce = self.attempt_nonce

        candidate_binary_sha256 = self.candidate_binary_sha256

        candidate_package_sha256 = self.candidate_package_sha256

        candidate_version = self.candidate_version

        created_at = self.created_at

        node_id = self.node_id

        outcome = self.outcome

        phase: str = self.phase

        schema_version = self.schema_version

        source_binary_sha256 = self.source_binary_sha256

        source_package_sha256 = self.source_package_sha256

        source_version = self.source_version

        updated_at = self.updated_at


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "attempt_nonce": attempt_nonce,
            "candidate_binary_sha256": candidate_binary_sha256,
            "candidate_package_sha256": candidate_package_sha256,
            "candidate_version": candidate_version,
            "created_at": created_at,
            "node_id": node_id,
            "outcome": outcome,
            "phase": phase,
            "schema_version": schema_version,
            "source_binary_sha256": source_binary_sha256,
            "source_package_sha256": source_package_sha256,
            "source_version": source_version,
            "updated_at": updated_at,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        attempt_nonce = d.pop("attempt_nonce")

        candidate_binary_sha256 = d.pop("candidate_binary_sha256")

        candidate_package_sha256 = d.pop("candidate_package_sha256")

        candidate_version = d.pop("candidate_version")

        created_at = d.pop("created_at")

        node_id = d.pop("node_id")

        outcome = d.pop("outcome")

        phase = check_package_activation_receipt_phase(d.pop("phase"))




        schema_version = cast(Literal[2] , d.pop("schema_version"))
        if schema_version != 2:
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        source_binary_sha256 = d.pop("source_binary_sha256")

        source_package_sha256 = d.pop("source_package_sha256")

        source_version = d.pop("source_version")

        updated_at = d.pop("updated_at")

        package_activation_receipt = cls(
            attempt_nonce=attempt_nonce,
            candidate_binary_sha256=candidate_binary_sha256,
            candidate_package_sha256=candidate_package_sha256,
            candidate_version=candidate_version,
            created_at=created_at,
            node_id=node_id,
            outcome=outcome,
            phase=phase,
            schema_version=schema_version,
            source_binary_sha256=source_binary_sha256,
            source_package_sha256=source_package_sha256,
            source_version=source_version,
            updated_at=updated_at,
        )

        return package_activation_receipt
