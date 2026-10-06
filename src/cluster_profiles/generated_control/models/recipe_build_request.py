from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.recipe_build_adapter import RecipeBuildAdapter
  from ..models.recipe_build_base_image import RecipeBuildBaseImage
  from ..models.recipe_build_limits import RecipeBuildLimits
  from ..models.recipe_build_network import RecipeBuildNetwork
  from ..models.recipe_build_options import RecipeBuildOptions





T = TypeVar("T", bound="RecipeBuildRequest")



@_attrs_define
class RecipeBuildRequest:
    """ Build one recipe image for linux/arm64.

        Attributes:
            adapter (RecipeBuildAdapter): The adaptation stage applied after the recipe image is built.

                ``adapter_sha256`` is the canonical digest of ``definition``.  The agent
                re-derives it from the received definition and refuses to adapt when they
                disagree, so a Controller/agent drift cannot silently install a different
                adaptation than the one the prepared-image identity recorded.
            base_image_storage_bytes (int):
            base_images (list[RecipeBuildBaseImage]):
            build_id (str):
            build_input_sha256 (str):
            capabilities (list[str]):
            dockerfile (str):
            limits (RecipeBuildLimits): Resource limits; builds never get a GPU, privileges, host mounts or a
                container socket.
            network (RecipeBuildNetwork): Public hosts the build may reach; an empty list builds offline.
            options (RecipeBuildOptions):
            recipe_content_sha256 (str):
            recipe_revision_id (str):
            source_bundle_bytes (int):
            source_bundle_sha256 (str):
     """

    adapter: RecipeBuildAdapter
    base_image_storage_bytes: int
    base_images: list[RecipeBuildBaseImage]
    build_id: str
    build_input_sha256: str
    capabilities: list[str]
    dockerfile: str
    limits: RecipeBuildLimits
    network: RecipeBuildNetwork
    options: RecipeBuildOptions
    recipe_content_sha256: str
    recipe_revision_id: str
    source_bundle_bytes: int
    source_bundle_sha256: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_build_adapter import RecipeBuildAdapter # noqa: PLC0415
        from ..models.recipe_build_base_image import RecipeBuildBaseImage # noqa: PLC0415
        from ..models.recipe_build_limits import RecipeBuildLimits # noqa: PLC0415
        from ..models.recipe_build_network import RecipeBuildNetwork # noqa: PLC0415
        from ..models.recipe_build_options import RecipeBuildOptions # noqa: PLC0415
        adapter = self.adapter.to_dict()

        base_image_storage_bytes = self.base_image_storage_bytes

        base_images = []
        for base_images_item_data in self.base_images:
            base_images_item = base_images_item_data.to_dict()
            base_images.append(base_images_item)



        build_id = self.build_id

        build_input_sha256 = self.build_input_sha256

        capabilities = self.capabilities



        dockerfile = self.dockerfile

        limits = self.limits.to_dict()

        network = self.network.to_dict()

        options = self.options.to_dict()

        recipe_content_sha256 = self.recipe_content_sha256

        recipe_revision_id = self.recipe_revision_id

        source_bundle_bytes = self.source_bundle_bytes

        source_bundle_sha256 = self.source_bundle_sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "adapter": adapter,
            "base_image_storage_bytes": base_image_storage_bytes,
            "base_images": base_images,
            "build_id": build_id,
            "build_input_sha256": build_input_sha256,
            "capabilities": capabilities,
            "dockerfile": dockerfile,
            "limits": limits,
            "network": network,
            "options": options,
            "recipe_content_sha256": recipe_content_sha256,
            "recipe_revision_id": recipe_revision_id,
            "source_bundle_bytes": source_bundle_bytes,
            "source_bundle_sha256": source_bundle_sha256,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_build_adapter import RecipeBuildAdapter # noqa: PLC0415
        from ..models.recipe_build_base_image import RecipeBuildBaseImage # noqa: PLC0415
        from ..models.recipe_build_limits import RecipeBuildLimits # noqa: PLC0415
        from ..models.recipe_build_network import RecipeBuildNetwork # noqa: PLC0415
        from ..models.recipe_build_options import RecipeBuildOptions # noqa: PLC0415
        d = dict(src_dict)
        adapter = RecipeBuildAdapter.from_dict(d.pop("adapter"))




        base_image_storage_bytes = d.pop("base_image_storage_bytes")

        base_images = []
        _base_images = d.pop("base_images")
        for base_images_item_data in (_base_images):
            base_images_item = RecipeBuildBaseImage.from_dict(base_images_item_data)



            base_images.append(base_images_item)


        build_id = d.pop("build_id")

        build_input_sha256 = d.pop("build_input_sha256")

        capabilities = cast(list[str], d.pop("capabilities"))


        dockerfile = d.pop("dockerfile")

        limits = RecipeBuildLimits.from_dict(d.pop("limits"))




        network = RecipeBuildNetwork.from_dict(d.pop("network"))




        options = RecipeBuildOptions.from_dict(d.pop("options"))




        recipe_content_sha256 = d.pop("recipe_content_sha256")

        recipe_revision_id = d.pop("recipe_revision_id")

        source_bundle_bytes = d.pop("source_bundle_bytes")

        source_bundle_sha256 = d.pop("source_bundle_sha256")

        recipe_build_request = cls(
            adapter=adapter,
            base_image_storage_bytes=base_image_storage_bytes,
            base_images=base_images,
            build_id=build_id,
            build_input_sha256=build_input_sha256,
            capabilities=capabilities,
            dockerfile=dockerfile,
            limits=limits,
            network=network,
            options=options,
            recipe_content_sha256=recipe_content_sha256,
            recipe_revision_id=recipe_revision_id,
            source_bundle_bytes=source_bundle_bytes,
            source_bundle_sha256=source_bundle_sha256,
        )

        return recipe_build_request
