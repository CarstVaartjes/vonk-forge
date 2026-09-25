from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="RecipeReconcileResult")



@_attrs_define
class RecipeReconcileResult:
    """
        Attributes:
            cleanup_receipt_sha256 (str):
            compiled_spec_canonical_sha256 (str):
            install_operation_id (str):
            install_operation_payload_sha256 (str):
            installation_id (str):
            node_id (str):
            plan_digest (str):
            recipe_content_sha256 (str):
            recipe_revision_id (str):
            reconciled (bool):
            removed_bytes (int): Measured bytes in the removed agent-owned installation tree, excluding the exact helper-
                managed runtime-cache subtree. The helper separately confirms removal of that private cache without reporting
                its byte count.
     """

    cleanup_receipt_sha256: str
    compiled_spec_canonical_sha256: str
    install_operation_id: str
    install_operation_payload_sha256: str
    installation_id: str
    node_id: str
    plan_digest: str
    recipe_content_sha256: str
    recipe_revision_id: str
    reconciled: bool
    removed_bytes: int





    def to_dict(self) -> dict[str, Any]:
        cleanup_receipt_sha256 = self.cleanup_receipt_sha256

        compiled_spec_canonical_sha256 = self.compiled_spec_canonical_sha256

        install_operation_id = self.install_operation_id

        install_operation_payload_sha256 = self.install_operation_payload_sha256

        installation_id = self.installation_id

        node_id = self.node_id

        plan_digest = self.plan_digest

        recipe_content_sha256 = self.recipe_content_sha256

        recipe_revision_id = self.recipe_revision_id

        reconciled = self.reconciled

        removed_bytes = self.removed_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "cleanup_receipt_sha256": cleanup_receipt_sha256,
            "compiled_spec_canonical_sha256": compiled_spec_canonical_sha256,
            "install_operation_id": install_operation_id,
            "install_operation_payload_sha256": install_operation_payload_sha256,
            "installation_id": installation_id,
            "node_id": node_id,
            "plan_digest": plan_digest,
            "recipe_content_sha256": recipe_content_sha256,
            "recipe_revision_id": recipe_revision_id,
            "reconciled": reconciled,
            "removed_bytes": removed_bytes,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        cleanup_receipt_sha256 = d.pop("cleanup_receipt_sha256")

        compiled_spec_canonical_sha256 = d.pop("compiled_spec_canonical_sha256")

        install_operation_id = d.pop("install_operation_id")

        install_operation_payload_sha256 = d.pop("install_operation_payload_sha256")

        installation_id = d.pop("installation_id")

        node_id = d.pop("node_id")

        plan_digest = d.pop("plan_digest")

        recipe_content_sha256 = d.pop("recipe_content_sha256")

        recipe_revision_id = d.pop("recipe_revision_id")

        reconciled = d.pop("reconciled")

        removed_bytes = d.pop("removed_bytes")

        recipe_reconcile_result = cls(
            cleanup_receipt_sha256=cleanup_receipt_sha256,
            compiled_spec_canonical_sha256=compiled_spec_canonical_sha256,
            install_operation_id=install_operation_id,
            install_operation_payload_sha256=install_operation_payload_sha256,
            installation_id=installation_id,
            node_id=node_id,
            plan_digest=plan_digest,
            recipe_content_sha256=recipe_content_sha256,
            recipe_revision_id=recipe_revision_id,
            reconciled=reconciled,
            removed_bytes=removed_bytes,
        )

        return recipe_reconcile_result
