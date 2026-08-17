from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_summary_response_lifecycle import check_recipe_summary_response_lifecycle
from ..models.recipe_summary_response_lifecycle import RecipeSummaryResponseLifecycle
from ..models.recipe_summary_response_origin import check_recipe_summary_response_origin
from ..models.recipe_summary_response_origin import RecipeSummaryResponseOrigin
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="RecipeSummaryResponse")



@_attrs_define
class RecipeSummaryResponse:
    """
        Attributes:
            artifact_count (int):
            execution_harness (str):
            expected_download_bytes (int):
            lifecycle (RecipeSummaryResponseLifecycle):
            maximum_installed_bytes_per_node (int):
            maximum_runtime_memory_bytes_per_node (int):
            node_count (int):
            origin (RecipeSummaryResponseOrigin):
            recipe_id (str):
            revision_number (int):
            runtime_distribution (str):
            slug (str):
            source_bundle_sha256 (str):
            title (str):
            topology_mode (str):
            topology_name (str):
            content_sha256 (Union[None, Unset, str]):
     """

    artifact_count: int
    execution_harness: str
    expected_download_bytes: int
    lifecycle: RecipeSummaryResponseLifecycle
    maximum_installed_bytes_per_node: int
    maximum_runtime_memory_bytes_per_node: int
    node_count: int
    origin: RecipeSummaryResponseOrigin
    recipe_id: str
    revision_number: int
    runtime_distribution: str
    slug: str
    source_bundle_sha256: str
    title: str
    topology_mode: str
    topology_name: str
    content_sha256: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        artifact_count = self.artifact_count

        execution_harness = self.execution_harness

        expected_download_bytes = self.expected_download_bytes

        lifecycle: str = self.lifecycle

        maximum_installed_bytes_per_node = self.maximum_installed_bytes_per_node

        maximum_runtime_memory_bytes_per_node = self.maximum_runtime_memory_bytes_per_node

        node_count = self.node_count

        origin: str = self.origin

        recipe_id = self.recipe_id

        revision_number = self.revision_number

        runtime_distribution = self.runtime_distribution

        slug = self.slug

        source_bundle_sha256 = self.source_bundle_sha256

        title = self.title

        topology_mode = self.topology_mode

        topology_name = self.topology_name

        content_sha256: Union[None, Unset, str]
        if isinstance(self.content_sha256, Unset):
            content_sha256 = UNSET
        else:
            content_sha256 = self.content_sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifact_count": artifact_count,
            "execution_harness": execution_harness,
            "expected_download_bytes": expected_download_bytes,
            "lifecycle": lifecycle,
            "maximum_installed_bytes_per_node": maximum_installed_bytes_per_node,
            "maximum_runtime_memory_bytes_per_node": maximum_runtime_memory_bytes_per_node,
            "node_count": node_count,
            "origin": origin,
            "recipe_id": recipe_id,
            "revision_number": revision_number,
            "runtime_distribution": runtime_distribution,
            "slug": slug,
            "source_bundle_sha256": source_bundle_sha256,
            "title": title,
            "topology_mode": topology_mode,
            "topology_name": topology_name,
        })
        if content_sha256 is not UNSET:
            field_dict["content_sha256"] = content_sha256

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        artifact_count = d.pop("artifact_count")

        execution_harness = d.pop("execution_harness")

        expected_download_bytes = d.pop("expected_download_bytes")

        lifecycle = check_recipe_summary_response_lifecycle(d.pop("lifecycle"))




        maximum_installed_bytes_per_node = d.pop("maximum_installed_bytes_per_node")

        maximum_runtime_memory_bytes_per_node = d.pop("maximum_runtime_memory_bytes_per_node")

        node_count = d.pop("node_count")

        origin = check_recipe_summary_response_origin(d.pop("origin"))




        recipe_id = d.pop("recipe_id")

        revision_number = d.pop("revision_number")

        runtime_distribution = d.pop("runtime_distribution")

        slug = d.pop("slug")

        source_bundle_sha256 = d.pop("source_bundle_sha256")

        title = d.pop("title")

        topology_mode = d.pop("topology_mode")

        topology_name = d.pop("topology_name")

        def _parse_content_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        content_sha256 = _parse_content_sha256(d.pop("content_sha256", UNSET))


        recipe_summary_response = cls(
            artifact_count=artifact_count,
            execution_harness=execution_harness,
            expected_download_bytes=expected_download_bytes,
            lifecycle=lifecycle,
            maximum_installed_bytes_per_node=maximum_installed_bytes_per_node,
            maximum_runtime_memory_bytes_per_node=maximum_runtime_memory_bytes_per_node,
            node_count=node_count,
            origin=origin,
            recipe_id=recipe_id,
            revision_number=revision_number,
            runtime_distribution=runtime_distribution,
            slug=slug,
            source_bundle_sha256=source_bundle_sha256,
            title=title,
            topology_mode=topology_mode,
            topology_name=topology_name,
            content_sha256=content_sha256,
        )

        return recipe_summary_response
