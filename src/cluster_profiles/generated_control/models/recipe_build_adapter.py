from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.recipe_build_adapter_definition import RecipeBuildAdapterDefinition





T = TypeVar("T", bound="RecipeBuildAdapter")



@_attrs_define
class RecipeBuildAdapter:
    """ The adaptation stage applied after the recipe image is built.

    ``adapter_sha256`` is the canonical digest of ``definition``.  The agent
    re-derives it from the received definition and refuses to adapt when they
    disagree, so a Controller/agent drift cannot silently install a different
    adaptation than the one the prepared-image identity recorded.

        Attributes:
            adapter_sha256 (str):
            definition (RecipeBuildAdapterDefinition): One reviewed, digest-identified platform adaptation of a built image.

                The recipe Dockerfile owns the pinned upstream runtime, compilation, model
                patches and engine arguments.  The platform owns the final Vonk contract
                layered on top of that built image: the ``ai.vonkforge.runtime-interface``
                label, the canonical ``/opt/vonk/bin/<engine>`` launcher and the runtime
                user/ownership.  Hosting that in one reviewed adapter instead of recipe
                boilerplate requires an identity that changes when the adaptation changes,
                so this definition -- not the compatible ``v1`` label -- is what the digest
                covers.  ``containerfile`` is the rendered, ordered adaptation stage and is
                the only content the builder executes.
     """

    adapter_sha256: str
    definition: RecipeBuildAdapterDefinition





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_build_adapter_definition import RecipeBuildAdapterDefinition # noqa: PLC0415
        adapter_sha256 = self.adapter_sha256

        definition = self.definition.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "adapter_sha256": adapter_sha256,
            "definition": definition,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_build_adapter_definition import RecipeBuildAdapterDefinition # noqa: PLC0415
        d = dict(src_dict)
        adapter_sha256 = d.pop("adapter_sha256")

        definition = RecipeBuildAdapterDefinition.from_dict(d.pop("definition"))




        recipe_build_adapter = cls(
            adapter_sha256=adapter_sha256,
            definition=definition,
        )

        return recipe_build_adapter
