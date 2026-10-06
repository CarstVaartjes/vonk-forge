from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="RecipeBuildAdapterDefinition")



@_attrs_define
class RecipeBuildAdapterDefinition:
    """ One reviewed, digest-identified platform adaptation of a built image.

    The recipe Dockerfile owns the pinned upstream runtime, compilation, model
    patches and engine arguments.  The platform owns the final Vonk contract
    layered on top of that built image: the ``ai.vonkforge.runtime-interface``
    label, the canonical ``/opt/vonk/bin/<engine>`` launcher and the runtime
    user/ownership.  Hosting that in one reviewed adapter instead of recipe
    boilerplate requires an identity that changes when the adaptation changes,
    so this definition -- not the compatible ``v1`` label -- is what the digest
    covers.  ``containerfile`` is the rendered, ordered adaptation stage and is
    the only content the builder executes.

        Attributes:
            adapter_id (str):
            containerfile (str):
            engine (str):
            image_user (str):
     """

    adapter_id: str
    containerfile: str
    engine: str
    image_user: str





    def to_dict(self) -> dict[str, Any]:
        adapter_id = self.adapter_id

        containerfile = self.containerfile

        engine = self.engine

        image_user = self.image_user


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "adapter_id": adapter_id,
            "containerfile": containerfile,
            "engine": engine,
            "image_user": image_user,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        adapter_id = d.pop("adapter_id")

        containerfile = d.pop("containerfile")

        engine = d.pop("engine")

        image_user = d.pop("image_user")

        recipe_build_adapter_definition = cls(
            adapter_id=adapter_id,
            containerfile=containerfile,
            engine=engine,
            image_user=image_user,
        )

        return recipe_build_adapter_definition
