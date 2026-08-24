from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.public_recipe_preview_response_capabilities_item import check_public_recipe_preview_response_capabilities_item
from ..models.public_recipe_preview_response_capabilities_item import PublicRecipePreviewResponseCapabilitiesItem
from ..models.public_recipe_preview_response_execution_readiness import check_public_recipe_preview_response_execution_readiness
from ..models.public_recipe_preview_response_execution_readiness import PublicRecipePreviewResponseExecutionReadiness
from ..models.public_recipe_preview_response_execution_readiness_basis import check_public_recipe_preview_response_execution_readiness_basis
from ..models.public_recipe_preview_response_execution_readiness_basis import PublicRecipePreviewResponseExecutionReadinessBasis
from ..models.public_recipe_preview_response_qualification import check_public_recipe_preview_response_qualification
from ..models.public_recipe_preview_response_qualification import PublicRecipePreviewResponseQualification
from ..models.public_recipe_preview_response_qualification_basis import check_public_recipe_preview_response_qualification_basis
from ..models.public_recipe_preview_response_qualification_basis import PublicRecipePreviewResponseQualificationBasis
from ..models.public_recipe_preview_response_source import check_public_recipe_preview_response_source
from ..models.public_recipe_preview_response_source import PublicRecipePreviewResponseSource
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.public_recipe_topology_role import PublicRecipeTopologyRole
  from ..models.public_recipe_fabric import PublicRecipeFabric
  from ..models.public_recipe_local_state import PublicRecipeLocalState
  from ..models.public_recipe_release import PublicRecipeRelease





T = TypeVar("T", bound="PublicRecipePreviewResponse")



@_attrs_define
class PublicRecipePreviewResponse:
    """
        Attributes:
            artifact_count (int):
            capabilities (list[PublicRecipePreviewResponseCapabilitiesItem]):
            changes_since_local (list['PublicRecipeRelease']):
            content_sha256 (str):
            description (str):
            execution_harness (str):
            execution_readiness (PublicRecipePreviewResponseExecutionReadiness):
            execution_readiness_basis (PublicRecipePreviewResponseExecutionReadinessBasis):
            execution_readiness_detail (str):
            expected_download_bytes (int):
            fabric (PublicRecipeFabric):
            local (PublicRecipeLocalState):
            maximum_installed_bytes_per_node (int):
            maximum_runtime_memory_bytes_per_node (int):
            model_publisher (str):
            model_slug (str):
            model_title (str):
            node_count (int):
            publisher (str):
            qualification (PublicRecipePreviewResponseQualification):
            qualification_basis (PublicRecipePreviewResponseQualificationBasis):
            qualification_detail (str):
            runtime_distribution (str):
            slug (str):
            source (PublicRecipePreviewResponseSource):
            source_bundle_sha256 (str):
            tags (list[str]):
            title (str):
            topology_mode (str):
            topology_name (str):
            topology_roles (list['PublicRecipeTopologyRole']):
            uri (str):
            precision (Union[None, Unset, str]):
            release_released_at (Union[None, Unset, str]):
            release_version (Union[None, Unset, str]):
            source_owner (Union[None, Unset, str]):
            source_repository (Union[None, Unset, str]):
     """

    artifact_count: int
    capabilities: list[PublicRecipePreviewResponseCapabilitiesItem]
    changes_since_local: list['PublicRecipeRelease']
    content_sha256: str
    description: str
    execution_harness: str
    execution_readiness: PublicRecipePreviewResponseExecutionReadiness
    execution_readiness_basis: PublicRecipePreviewResponseExecutionReadinessBasis
    execution_readiness_detail: str
    expected_download_bytes: int
    fabric: 'PublicRecipeFabric'
    local: 'PublicRecipeLocalState'
    maximum_installed_bytes_per_node: int
    maximum_runtime_memory_bytes_per_node: int
    model_publisher: str
    model_slug: str
    model_title: str
    node_count: int
    publisher: str
    qualification: PublicRecipePreviewResponseQualification
    qualification_basis: PublicRecipePreviewResponseQualificationBasis
    qualification_detail: str
    runtime_distribution: str
    slug: str
    source: PublicRecipePreviewResponseSource
    source_bundle_sha256: str
    tags: list[str]
    title: str
    topology_mode: str
    topology_name: str
    topology_roles: list['PublicRecipeTopologyRole']
    uri: str
    precision: Union[None, Unset, str] = UNSET
    release_released_at: Union[None, Unset, str] = UNSET
    release_version: Union[None, Unset, str] = UNSET
    source_owner: Union[None, Unset, str] = UNSET
    source_repository: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.public_recipe_topology_role import PublicRecipeTopologyRole
        from ..models.public_recipe_fabric import PublicRecipeFabric
        from ..models.public_recipe_local_state import PublicRecipeLocalState
        from ..models.public_recipe_release import PublicRecipeRelease
        artifact_count = self.artifact_count

        capabilities = []
        for capabilities_item_data in self.capabilities:
            capabilities_item: str = capabilities_item_data
            capabilities.append(capabilities_item)



        changes_since_local = []
        for changes_since_local_item_data in self.changes_since_local:
            changes_since_local_item = changes_since_local_item_data.to_dict()
            changes_since_local.append(changes_since_local_item)



        content_sha256 = self.content_sha256

        description = self.description

        execution_harness = self.execution_harness

        execution_readiness: str = self.execution_readiness

        execution_readiness_basis: str = self.execution_readiness_basis

        execution_readiness_detail = self.execution_readiness_detail

        expected_download_bytes = self.expected_download_bytes

        fabric = self.fabric.to_dict()

        local = self.local.to_dict()

        maximum_installed_bytes_per_node = self.maximum_installed_bytes_per_node

        maximum_runtime_memory_bytes_per_node = self.maximum_runtime_memory_bytes_per_node

        model_publisher = self.model_publisher

        model_slug = self.model_slug

        model_title = self.model_title

        node_count = self.node_count

        publisher = self.publisher

        qualification: str = self.qualification

        qualification_basis: str = self.qualification_basis

        qualification_detail = self.qualification_detail

        runtime_distribution = self.runtime_distribution

        slug = self.slug

        source: str = self.source

        source_bundle_sha256 = self.source_bundle_sha256

        tags = self.tags



        title = self.title

        topology_mode = self.topology_mode

        topology_name = self.topology_name

        topology_roles = []
        for topology_roles_item_data in self.topology_roles:
            topology_roles_item = topology_roles_item_data.to_dict()
            topology_roles.append(topology_roles_item)



        uri = self.uri

        precision: Union[None, Unset, str]
        if isinstance(self.precision, Unset):
            precision = UNSET
        else:
            precision = self.precision

        release_released_at: Union[None, Unset, str]
        if isinstance(self.release_released_at, Unset):
            release_released_at = UNSET
        else:
            release_released_at = self.release_released_at

        release_version: Union[None, Unset, str]
        if isinstance(self.release_version, Unset):
            release_version = UNSET
        else:
            release_version = self.release_version

        source_owner: Union[None, Unset, str]
        if isinstance(self.source_owner, Unset):
            source_owner = UNSET
        else:
            source_owner = self.source_owner

        source_repository: Union[None, Unset, str]
        if isinstance(self.source_repository, Unset):
            source_repository = UNSET
        else:
            source_repository = self.source_repository


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifact_count": artifact_count,
            "capabilities": capabilities,
            "changes_since_local": changes_since_local,
            "content_sha256": content_sha256,
            "description": description,
            "execution_harness": execution_harness,
            "execution_readiness": execution_readiness,
            "execution_readiness_basis": execution_readiness_basis,
            "execution_readiness_detail": execution_readiness_detail,
            "expected_download_bytes": expected_download_bytes,
            "fabric": fabric,
            "local": local,
            "maximum_installed_bytes_per_node": maximum_installed_bytes_per_node,
            "maximum_runtime_memory_bytes_per_node": maximum_runtime_memory_bytes_per_node,
            "model_publisher": model_publisher,
            "model_slug": model_slug,
            "model_title": model_title,
            "node_count": node_count,
            "publisher": publisher,
            "qualification": qualification,
            "qualification_basis": qualification_basis,
            "qualification_detail": qualification_detail,
            "runtime_distribution": runtime_distribution,
            "slug": slug,
            "source": source,
            "source_bundle_sha256": source_bundle_sha256,
            "tags": tags,
            "title": title,
            "topology_mode": topology_mode,
            "topology_name": topology_name,
            "topology_roles": topology_roles,
            "uri": uri,
        })
        if precision is not UNSET:
            field_dict["precision"] = precision
        if release_released_at is not UNSET:
            field_dict["release_released_at"] = release_released_at
        if release_version is not UNSET:
            field_dict["release_version"] = release_version
        if source_owner is not UNSET:
            field_dict["source_owner"] = source_owner
        if source_repository is not UNSET:
            field_dict["source_repository"] = source_repository

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.public_recipe_topology_role import PublicRecipeTopologyRole
        from ..models.public_recipe_fabric import PublicRecipeFabric
        from ..models.public_recipe_local_state import PublicRecipeLocalState
        from ..models.public_recipe_release import PublicRecipeRelease
        d = dict(src_dict)
        artifact_count = d.pop("artifact_count")

        capabilities = []
        _capabilities = d.pop("capabilities")
        for capabilities_item_data in (_capabilities):
            capabilities_item = check_public_recipe_preview_response_capabilities_item(capabilities_item_data)



            capabilities.append(capabilities_item)


        changes_since_local = []
        _changes_since_local = d.pop("changes_since_local")
        for changes_since_local_item_data in (_changes_since_local):
            changes_since_local_item = PublicRecipeRelease.from_dict(changes_since_local_item_data)



            changes_since_local.append(changes_since_local_item)


        content_sha256 = d.pop("content_sha256")

        description = d.pop("description")

        execution_harness = d.pop("execution_harness")

        execution_readiness = check_public_recipe_preview_response_execution_readiness(d.pop("execution_readiness"))




        execution_readiness_basis = check_public_recipe_preview_response_execution_readiness_basis(d.pop("execution_readiness_basis"))




        execution_readiness_detail = d.pop("execution_readiness_detail")

        expected_download_bytes = d.pop("expected_download_bytes")

        fabric = PublicRecipeFabric.from_dict(d.pop("fabric"))




        local = PublicRecipeLocalState.from_dict(d.pop("local"))




        maximum_installed_bytes_per_node = d.pop("maximum_installed_bytes_per_node")

        maximum_runtime_memory_bytes_per_node = d.pop("maximum_runtime_memory_bytes_per_node")

        model_publisher = d.pop("model_publisher")

        model_slug = d.pop("model_slug")

        model_title = d.pop("model_title")

        node_count = d.pop("node_count")

        publisher = d.pop("publisher")

        qualification = check_public_recipe_preview_response_qualification(d.pop("qualification"))




        qualification_basis = check_public_recipe_preview_response_qualification_basis(d.pop("qualification_basis"))




        qualification_detail = d.pop("qualification_detail")

        runtime_distribution = d.pop("runtime_distribution")

        slug = d.pop("slug")

        source = check_public_recipe_preview_response_source(d.pop("source"))




        source_bundle_sha256 = d.pop("source_bundle_sha256")

        tags = cast(list[str], d.pop("tags"))


        title = d.pop("title")

        topology_mode = d.pop("topology_mode")

        topology_name = d.pop("topology_name")

        topology_roles = []
        _topology_roles = d.pop("topology_roles")
        for topology_roles_item_data in (_topology_roles):
            topology_roles_item = PublicRecipeTopologyRole.from_dict(topology_roles_item_data)



            topology_roles.append(topology_roles_item)


        uri = d.pop("uri")

        def _parse_precision(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        precision = _parse_precision(d.pop("precision", UNSET))


        def _parse_release_released_at(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        release_released_at = _parse_release_released_at(d.pop("release_released_at", UNSET))


        def _parse_release_version(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        release_version = _parse_release_version(d.pop("release_version", UNSET))


        def _parse_source_owner(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        source_owner = _parse_source_owner(d.pop("source_owner", UNSET))


        def _parse_source_repository(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        source_repository = _parse_source_repository(d.pop("source_repository", UNSET))


        public_recipe_preview_response = cls(
            artifact_count=artifact_count,
            capabilities=capabilities,
            changes_since_local=changes_since_local,
            content_sha256=content_sha256,
            description=description,
            execution_harness=execution_harness,
            execution_readiness=execution_readiness,
            execution_readiness_basis=execution_readiness_basis,
            execution_readiness_detail=execution_readiness_detail,
            expected_download_bytes=expected_download_bytes,
            fabric=fabric,
            local=local,
            maximum_installed_bytes_per_node=maximum_installed_bytes_per_node,
            maximum_runtime_memory_bytes_per_node=maximum_runtime_memory_bytes_per_node,
            model_publisher=model_publisher,
            model_slug=model_slug,
            model_title=model_title,
            node_count=node_count,
            publisher=publisher,
            qualification=qualification,
            qualification_basis=qualification_basis,
            qualification_detail=qualification_detail,
            runtime_distribution=runtime_distribution,
            slug=slug,
            source=source,
            source_bundle_sha256=source_bundle_sha256,
            tags=tags,
            title=title,
            topology_mode=topology_mode,
            topology_name=topology_name,
            topology_roles=topology_roles,
            uri=uri,
            precision=precision,
            release_released_at=release_released_at,
            release_version=release_version,
            source_owner=source_owner,
            source_repository=source_repository,
        )

        return public_recipe_preview_response
