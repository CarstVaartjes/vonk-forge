from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.public_recipe_list_item_capabilities_item import check_public_recipe_list_item_capabilities_item
from ..models.public_recipe_list_item_capabilities_item import PublicRecipeListItemCapabilitiesItem
from ..models.public_recipe_list_item_qualification import check_public_recipe_list_item_qualification
from ..models.public_recipe_list_item_qualification import PublicRecipeListItemQualification
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="PublicRecipeListItem")



@_attrs_define
class PublicRecipeListItem:
    """
        Attributes:
            artifact_count (int):
            capabilities (list[PublicRecipeListItemCapabilitiesItem]):
            content_sha256 (str):
            description (str):
            execution_harness (str):
            expected_download_bytes (int):
            maximum_installed_bytes_per_node (int):
            maximum_runtime_memory_bytes_per_node (int):
            model_publisher (str):
            model_slug (str):
            model_title (str):
            node_count (int):
            publisher (str):
            qualification (PublicRecipeListItemQualification):
            runtime_distribution (str):
            slug (str):
            source_bundle_sha256 (str):
            tags (list[str]):
            title (str):
            topology_mode (str):
            topology_name (str):
            uri (str):
            precision (Union[None, Unset, str]):
     """

    artifact_count: int
    capabilities: list[PublicRecipeListItemCapabilitiesItem]
    content_sha256: str
    description: str
    execution_harness: str
    expected_download_bytes: int
    maximum_installed_bytes_per_node: int
    maximum_runtime_memory_bytes_per_node: int
    model_publisher: str
    model_slug: str
    model_title: str
    node_count: int
    publisher: str
    qualification: PublicRecipeListItemQualification
    runtime_distribution: str
    slug: str
    source_bundle_sha256: str
    tags: list[str]
    title: str
    topology_mode: str
    topology_name: str
    uri: str
    precision: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        artifact_count = self.artifact_count

        capabilities = []
        for capabilities_item_data in self.capabilities:
            capabilities_item: str = capabilities_item_data
            capabilities.append(capabilities_item)



        content_sha256 = self.content_sha256

        description = self.description

        execution_harness = self.execution_harness

        expected_download_bytes = self.expected_download_bytes

        maximum_installed_bytes_per_node = self.maximum_installed_bytes_per_node

        maximum_runtime_memory_bytes_per_node = self.maximum_runtime_memory_bytes_per_node

        model_publisher = self.model_publisher

        model_slug = self.model_slug

        model_title = self.model_title

        node_count = self.node_count

        publisher = self.publisher

        qualification: str = self.qualification

        runtime_distribution = self.runtime_distribution

        slug = self.slug

        source_bundle_sha256 = self.source_bundle_sha256

        tags = self.tags



        title = self.title

        topology_mode = self.topology_mode

        topology_name = self.topology_name

        uri = self.uri

        precision: Union[None, Unset, str]
        if isinstance(self.precision, Unset):
            precision = UNSET
        else:
            precision = self.precision


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifact_count": artifact_count,
            "capabilities": capabilities,
            "content_sha256": content_sha256,
            "description": description,
            "execution_harness": execution_harness,
            "expected_download_bytes": expected_download_bytes,
            "maximum_installed_bytes_per_node": maximum_installed_bytes_per_node,
            "maximum_runtime_memory_bytes_per_node": maximum_runtime_memory_bytes_per_node,
            "model_publisher": model_publisher,
            "model_slug": model_slug,
            "model_title": model_title,
            "node_count": node_count,
            "publisher": publisher,
            "qualification": qualification,
            "runtime_distribution": runtime_distribution,
            "slug": slug,
            "source_bundle_sha256": source_bundle_sha256,
            "tags": tags,
            "title": title,
            "topology_mode": topology_mode,
            "topology_name": topology_name,
            "uri": uri,
        })
        if precision is not UNSET:
            field_dict["precision"] = precision

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        artifact_count = d.pop("artifact_count")

        capabilities = []
        _capabilities = d.pop("capabilities")
        for capabilities_item_data in (_capabilities):
            capabilities_item = check_public_recipe_list_item_capabilities_item(capabilities_item_data)



            capabilities.append(capabilities_item)


        content_sha256 = d.pop("content_sha256")

        description = d.pop("description")

        execution_harness = d.pop("execution_harness")

        expected_download_bytes = d.pop("expected_download_bytes")

        maximum_installed_bytes_per_node = d.pop("maximum_installed_bytes_per_node")

        maximum_runtime_memory_bytes_per_node = d.pop("maximum_runtime_memory_bytes_per_node")

        model_publisher = d.pop("model_publisher")

        model_slug = d.pop("model_slug")

        model_title = d.pop("model_title")

        node_count = d.pop("node_count")

        publisher = d.pop("publisher")

        qualification = check_public_recipe_list_item_qualification(d.pop("qualification"))




        runtime_distribution = d.pop("runtime_distribution")

        slug = d.pop("slug")

        source_bundle_sha256 = d.pop("source_bundle_sha256")

        tags = cast(list[str], d.pop("tags"))


        title = d.pop("title")

        topology_mode = d.pop("topology_mode")

        topology_name = d.pop("topology_name")

        uri = d.pop("uri")

        def _parse_precision(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        precision = _parse_precision(d.pop("precision", UNSET))


        public_recipe_list_item = cls(
            artifact_count=artifact_count,
            capabilities=capabilities,
            content_sha256=content_sha256,
            description=description,
            execution_harness=execution_harness,
            expected_download_bytes=expected_download_bytes,
            maximum_installed_bytes_per_node=maximum_installed_bytes_per_node,
            maximum_runtime_memory_bytes_per_node=maximum_runtime_memory_bytes_per_node,
            model_publisher=model_publisher,
            model_slug=model_slug,
            model_title=model_title,
            node_count=node_count,
            publisher=publisher,
            qualification=qualification,
            runtime_distribution=runtime_distribution,
            slug=slug,
            source_bundle_sha256=source_bundle_sha256,
            tags=tags,
            title=title,
            topology_mode=topology_mode,
            topology_name=topology_name,
            uri=uri,
            precision=precision,
        )

        return public_recipe_list_item
