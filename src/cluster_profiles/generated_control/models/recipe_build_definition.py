from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.build_argument import BuildArgument
  from ..models.build_context import BuildContext
  from ..models.build_network import BuildNetwork
  from ..models.build_patch import BuildPatch
  from ..models.recipe_image import RecipeImage





T = TypeVar("T", bound="RecipeBuildDefinition")



@_attrs_define
class RecipeBuildDefinition:
    """
        Attributes:
            arguments (list[BuildArgument]):
            base_image (RecipeImage):
            context (BuildContext):
            dockerfile (str):
            network (BuildNetwork):
            patches (list[BuildPatch]):
            target (None | str | Unset):
     """

    arguments: list[BuildArgument]
    base_image: RecipeImage
    context: BuildContext
    dockerfile: str
    network: BuildNetwork
    patches: list[BuildPatch]
    target: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.build_argument import BuildArgument # noqa: PLC0415
        from ..models.build_context import BuildContext # noqa: PLC0415
        from ..models.build_network import BuildNetwork # noqa: PLC0415
        from ..models.build_patch import BuildPatch # noqa: PLC0415
        from ..models.recipe_image import RecipeImage # noqa: PLC0415
        arguments = []
        for arguments_item_data in self.arguments:
            arguments_item = arguments_item_data.to_dict()
            arguments.append(arguments_item)



        base_image = self.base_image.to_dict()

        context = self.context.to_dict()

        dockerfile = self.dockerfile

        network = self.network.to_dict()

        patches = []
        for patches_item_data in self.patches:
            patches_item = patches_item_data.to_dict()
            patches.append(patches_item)



        target: None | str | Unset
        if isinstance(self.target, Unset):
            target = UNSET
        else:
            target = self.target


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "arguments": arguments,
            "base_image": base_image,
            "context": context,
            "dockerfile": dockerfile,
            "network": network,
            "patches": patches,
        })
        if target is not UNSET:
            field_dict["target"] = target

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.build_argument import BuildArgument # noqa: PLC0415
        from ..models.build_context import BuildContext # noqa: PLC0415
        from ..models.build_network import BuildNetwork # noqa: PLC0415
        from ..models.build_patch import BuildPatch # noqa: PLC0415
        from ..models.recipe_image import RecipeImage # noqa: PLC0415
        d = dict(src_dict)
        arguments = []
        _arguments = d.pop("arguments")
        for arguments_item_data in (_arguments):
            arguments_item = BuildArgument.from_dict(arguments_item_data)



            arguments.append(arguments_item)


        base_image = RecipeImage.from_dict(d.pop("base_image"))




        context = BuildContext.from_dict(d.pop("context"))




        dockerfile = d.pop("dockerfile")

        network = BuildNetwork.from_dict(d.pop("network"))




        patches = []
        _patches = d.pop("patches")
        for patches_item_data in (_patches):
            patches_item = BuildPatch.from_dict(patches_item_data)



            patches.append(patches_item)


        def _parse_target(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        target = _parse_target(d.pop("target", UNSET))


        recipe_build_definition = cls(
            arguments=arguments,
            base_image=base_image,
            context=context,
            dockerfile=dockerfile,
            network=network,
            patches=patches,
            target=target,
        )

        return recipe_build_definition
