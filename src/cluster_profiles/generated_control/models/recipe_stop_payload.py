from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset






T = TypeVar("T", bound="RecipeStopPayload")



@_attrs_define
class RecipeStopPayload:
    """ Exact cleanup authority, independent of historical launch-plan readability.

    The Controller binds these identities and timeout into the signed helper
    grant; the helper reconciles only the matching runtime generation.

        Attributes:
            installation_id (str):
            mapping_id (str):
            plan_digest (str):
            rank (int):
            recipe_content_sha256 (str):
            recipe_revision_id (str):
            role (str):
            run_generation (int):
            run_id (str):
            stop_timeout_seconds (int):
            target_runtime_id (str):
            cancel_pending_start (bool | Unset):  Default: False.
     """

    installation_id: str
    mapping_id: str
    plan_digest: str
    rank: int
    recipe_content_sha256: str
    recipe_revision_id: str
    role: str
    run_generation: int
    run_id: str
    stop_timeout_seconds: int
    target_runtime_id: str
    cancel_pending_start: bool | Unset = False





    def to_dict(self) -> dict[str, Any]:
        installation_id = self.installation_id

        mapping_id = self.mapping_id

        plan_digest = self.plan_digest

        rank = self.rank

        recipe_content_sha256 = self.recipe_content_sha256

        recipe_revision_id = self.recipe_revision_id

        role = self.role

        run_generation = self.run_generation

        run_id = self.run_id

        stop_timeout_seconds = self.stop_timeout_seconds

        target_runtime_id = self.target_runtime_id

        cancel_pending_start = self.cancel_pending_start


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "installation_id": installation_id,
            "mapping_id": mapping_id,
            "plan_digest": plan_digest,
            "rank": rank,
            "recipe_content_sha256": recipe_content_sha256,
            "recipe_revision_id": recipe_revision_id,
            "role": role,
            "run_generation": run_generation,
            "run_id": run_id,
            "stop_timeout_seconds": stop_timeout_seconds,
            "target_runtime_id": target_runtime_id,
        })
        if cancel_pending_start is not UNSET:
            field_dict["cancel_pending_start"] = cancel_pending_start

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        installation_id = d.pop("installation_id")

        mapping_id = d.pop("mapping_id")

        plan_digest = d.pop("plan_digest")

        rank = d.pop("rank")

        recipe_content_sha256 = d.pop("recipe_content_sha256")

        recipe_revision_id = d.pop("recipe_revision_id")

        role = d.pop("role")

        run_generation = d.pop("run_generation")

        run_id = d.pop("run_id")

        stop_timeout_seconds = d.pop("stop_timeout_seconds")

        target_runtime_id = d.pop("target_runtime_id")

        cancel_pending_start = d.pop("cancel_pending_start", UNSET)

        recipe_stop_payload = cls(
            installation_id=installation_id,
            mapping_id=mapping_id,
            plan_digest=plan_digest,
            rank=rank,
            recipe_content_sha256=recipe_content_sha256,
            recipe_revision_id=recipe_revision_id,
            role=role,
            run_generation=run_generation,
            run_id=run_id,
            stop_timeout_seconds=stop_timeout_seconds,
            target_runtime_id=target_runtime_id,
            cancel_pending_start=cancel_pending_start,
        )

        return recipe_stop_payload
