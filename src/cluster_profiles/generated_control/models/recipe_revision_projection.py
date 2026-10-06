from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.artifact_input_projection import ArtifactInputProjection
  from ..models.build_model_artifact_projection import BuildModelArtifactProjection
  from ..models.build_resources_projection import BuildResourcesProjection
  from ..models.build_security_projection import BuildSecurityProjection
  from ..models.prebuilt_image import PrebuiltImage
  from ..models.recipe_build_options import RecipeBuildOptions
  from ..models.recipe_package_handle_projection import RecipePackageHandleProjection
  from ..models.recipe_topology import RecipeTopology





T = TypeVar("T", bound="RecipeRevisionProjection")



@_attrs_define
class RecipeRevisionProjection:
    """
        Attributes:
            description (str):
            runtime_engine (str):
            tags (list[str]):
            title (str):
            topology (RecipeTopology): Roles and their start order; everything else follows from node_count.

                One node runs alone. More nodes share one connected fabric: losing a rank
                withdraws the endpoint, recovery restarts the workers and then the
                entrypoint, and stopping always starts with the endpoint owner.
            artifact_inputs (list[ArtifactInputProjection] | None | Unset):
            build_model_artifacts (list[BuildModelArtifactProjection] | None | Unset):
            build_options (None | RecipeBuildOptions | Unset):
            build_resources (BuildResourcesProjection | None | Unset):
            build_security (BuildSecurityProjection | None | Unset):
            failure_reason (None | str | Unset):
            package_handle (None | RecipePackageHandleProjection | Unset):
            package_sha256 (None | str | Unset):
            prebuilt_image (None | PrebuiltImage | Unset):
            publication_commit (None | str | Unset):
            release_released_at (None | str | Unset):
            release_version (None | str | Unset):
            source_bundle_sha256 (None | str | Unset):
            source_path (None | str | Unset):
     """

    description: str
    runtime_engine: str
    tags: list[str]
    title: str
    topology: RecipeTopology
    artifact_inputs: list[ArtifactInputProjection] | None | Unset = UNSET
    build_model_artifacts: list[BuildModelArtifactProjection] | None | Unset = UNSET
    build_options: None | RecipeBuildOptions | Unset = UNSET
    build_resources: BuildResourcesProjection | None | Unset = UNSET
    build_security: BuildSecurityProjection | None | Unset = UNSET
    failure_reason: None | str | Unset = UNSET
    package_handle: None | RecipePackageHandleProjection | Unset = UNSET
    package_sha256: None | str | Unset = UNSET
    prebuilt_image: None | PrebuiltImage | Unset = UNSET
    publication_commit: None | str | Unset = UNSET
    release_released_at: None | str | Unset = UNSET
    release_version: None | str | Unset = UNSET
    source_bundle_sha256: None | str | Unset = UNSET
    source_path: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.artifact_input_projection import ArtifactInputProjection # noqa: PLC0415
        from ..models.build_model_artifact_projection import BuildModelArtifactProjection # noqa: PLC0415
        from ..models.build_resources_projection import BuildResourcesProjection # noqa: PLC0415
        from ..models.build_security_projection import BuildSecurityProjection # noqa: PLC0415
        from ..models.prebuilt_image import PrebuiltImage # noqa: PLC0415
        from ..models.recipe_build_options import RecipeBuildOptions # noqa: PLC0415
        from ..models.recipe_package_handle_projection import RecipePackageHandleProjection # noqa: PLC0415
        from ..models.recipe_topology import RecipeTopology # noqa: PLC0415
        description = self.description

        runtime_engine = self.runtime_engine

        tags = self.tags



        title = self.title

        topology = self.topology.to_dict()

        artifact_inputs: list[dict[str, Any]] | None | Unset
        if isinstance(self.artifact_inputs, Unset):
            artifact_inputs = UNSET
        elif isinstance(self.artifact_inputs, list):
            artifact_inputs = []
            for artifact_inputs_type_0_item_data in self.artifact_inputs:
                artifact_inputs_type_0_item = artifact_inputs_type_0_item_data.to_dict()
                artifact_inputs.append(artifact_inputs_type_0_item)


        else:
            artifact_inputs = self.artifact_inputs

        build_model_artifacts: list[dict[str, Any]] | None | Unset
        if isinstance(self.build_model_artifacts, Unset):
            build_model_artifacts = UNSET
        elif isinstance(self.build_model_artifacts, list):
            build_model_artifacts = []
            for build_model_artifacts_type_0_item_data in self.build_model_artifacts:
                build_model_artifacts_type_0_item = build_model_artifacts_type_0_item_data.to_dict()
                build_model_artifacts.append(build_model_artifacts_type_0_item)


        else:
            build_model_artifacts = self.build_model_artifacts

        build_options: dict[str, Any] | None | Unset
        if isinstance(self.build_options, Unset):
            build_options = UNSET
        elif isinstance(self.build_options, RecipeBuildOptions):
            build_options = self.build_options.to_dict()
        else:
            build_options = self.build_options

        build_resources: dict[str, Any] | None | Unset
        if isinstance(self.build_resources, Unset):
            build_resources = UNSET
        elif isinstance(self.build_resources, BuildResourcesProjection):
            build_resources = self.build_resources.to_dict()
        else:
            build_resources = self.build_resources

        build_security: dict[str, Any] | None | Unset
        if isinstance(self.build_security, Unset):
            build_security = UNSET
        elif isinstance(self.build_security, BuildSecurityProjection):
            build_security = self.build_security.to_dict()
        else:
            build_security = self.build_security

        failure_reason: None | str | Unset
        if isinstance(self.failure_reason, Unset):
            failure_reason = UNSET
        else:
            failure_reason = self.failure_reason

        package_handle: dict[str, Any] | None | Unset
        if isinstance(self.package_handle, Unset):
            package_handle = UNSET
        elif isinstance(self.package_handle, RecipePackageHandleProjection):
            package_handle = self.package_handle.to_dict()
        else:
            package_handle = self.package_handle

        package_sha256: None | str | Unset
        if isinstance(self.package_sha256, Unset):
            package_sha256 = UNSET
        else:
            package_sha256 = self.package_sha256

        prebuilt_image: dict[str, Any] | None | Unset
        if isinstance(self.prebuilt_image, Unset):
            prebuilt_image = UNSET
        elif isinstance(self.prebuilt_image, PrebuiltImage):
            prebuilt_image = self.prebuilt_image.to_dict()
        else:
            prebuilt_image = self.prebuilt_image

        publication_commit: None | str | Unset
        if isinstance(self.publication_commit, Unset):
            publication_commit = UNSET
        else:
            publication_commit = self.publication_commit

        release_released_at: None | str | Unset
        if isinstance(self.release_released_at, Unset):
            release_released_at = UNSET
        else:
            release_released_at = self.release_released_at

        release_version: None | str | Unset
        if isinstance(self.release_version, Unset):
            release_version = UNSET
        else:
            release_version = self.release_version

        source_bundle_sha256: None | str | Unset
        if isinstance(self.source_bundle_sha256, Unset):
            source_bundle_sha256 = UNSET
        else:
            source_bundle_sha256 = self.source_bundle_sha256

        source_path: None | str | Unset
        if isinstance(self.source_path, Unset):
            source_path = UNSET
        else:
            source_path = self.source_path


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "description": description,
            "runtime_engine": runtime_engine,
            "tags": tags,
            "title": title,
            "topology": topology,
        })
        if artifact_inputs is not UNSET:
            field_dict["artifact_inputs"] = artifact_inputs
        if build_model_artifacts is not UNSET:
            field_dict["build_model_artifacts"] = build_model_artifacts
        if build_options is not UNSET:
            field_dict["build_options"] = build_options
        if build_resources is not UNSET:
            field_dict["build_resources"] = build_resources
        if build_security is not UNSET:
            field_dict["build_security"] = build_security
        if failure_reason is not UNSET:
            field_dict["failure_reason"] = failure_reason
        if package_handle is not UNSET:
            field_dict["package_handle"] = package_handle
        if package_sha256 is not UNSET:
            field_dict["package_sha256"] = package_sha256
        if prebuilt_image is not UNSET:
            field_dict["prebuilt_image"] = prebuilt_image
        if publication_commit is not UNSET:
            field_dict["publication_commit"] = publication_commit
        if release_released_at is not UNSET:
            field_dict["release_released_at"] = release_released_at
        if release_version is not UNSET:
            field_dict["release_version"] = release_version
        if source_bundle_sha256 is not UNSET:
            field_dict["source_bundle_sha256"] = source_bundle_sha256
        if source_path is not UNSET:
            field_dict["source_path"] = source_path

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.artifact_input_projection import ArtifactInputProjection # noqa: PLC0415
        from ..models.build_model_artifact_projection import BuildModelArtifactProjection # noqa: PLC0415
        from ..models.build_resources_projection import BuildResourcesProjection # noqa: PLC0415
        from ..models.build_security_projection import BuildSecurityProjection # noqa: PLC0415
        from ..models.prebuilt_image import PrebuiltImage # noqa: PLC0415
        from ..models.recipe_build_options import RecipeBuildOptions # noqa: PLC0415
        from ..models.recipe_package_handle_projection import RecipePackageHandleProjection # noqa: PLC0415
        from ..models.recipe_topology import RecipeTopology # noqa: PLC0415
        d = dict(src_dict)
        description = d.pop("description")

        runtime_engine = d.pop("runtime_engine")

        tags = cast(list[str], d.pop("tags"))


        title = d.pop("title")

        topology = RecipeTopology.from_dict(d.pop("topology"))




        def _parse_artifact_inputs(data: object) -> list[ArtifactInputProjection] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                artifact_inputs_type_0 = []
                _artifact_inputs_type_0 = data
                for artifact_inputs_type_0_item_data in (_artifact_inputs_type_0):
                    artifact_inputs_type_0_item = ArtifactInputProjection.from_dict(artifact_inputs_type_0_item_data)



                    artifact_inputs_type_0.append(artifact_inputs_type_0_item)

                return artifact_inputs_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[ArtifactInputProjection] | None | Unset, data)

        artifact_inputs = _parse_artifact_inputs(d.pop("artifact_inputs", UNSET))


        def _parse_build_model_artifacts(data: object) -> list[BuildModelArtifactProjection] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                build_model_artifacts_type_0 = []
                _build_model_artifacts_type_0 = data
                for build_model_artifacts_type_0_item_data in (_build_model_artifacts_type_0):
                    build_model_artifacts_type_0_item = BuildModelArtifactProjection.from_dict(build_model_artifacts_type_0_item_data)



                    build_model_artifacts_type_0.append(build_model_artifacts_type_0_item)

                return build_model_artifacts_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[BuildModelArtifactProjection] | None | Unset, data)

        build_model_artifacts = _parse_build_model_artifacts(d.pop("build_model_artifacts", UNSET))


        def _parse_build_options(data: object) -> None | RecipeBuildOptions | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                build_options_type_0 = RecipeBuildOptions.from_dict(data)



                return build_options_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RecipeBuildOptions | Unset, data)

        build_options = _parse_build_options(d.pop("build_options", UNSET))


        def _parse_build_resources(data: object) -> BuildResourcesProjection | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                build_resources_type_0 = BuildResourcesProjection.from_dict(data)



                return build_resources_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(BuildResourcesProjection | None | Unset, data)

        build_resources = _parse_build_resources(d.pop("build_resources", UNSET))


        def _parse_build_security(data: object) -> BuildSecurityProjection | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                build_security_type_0 = BuildSecurityProjection.from_dict(data)



                return build_security_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(BuildSecurityProjection | None | Unset, data)

        build_security = _parse_build_security(d.pop("build_security", UNSET))


        def _parse_failure_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        failure_reason = _parse_failure_reason(d.pop("failure_reason", UNSET))


        def _parse_package_handle(data: object) -> None | RecipePackageHandleProjection | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                package_handle_type_0 = RecipePackageHandleProjection.from_dict(data)



                return package_handle_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RecipePackageHandleProjection | Unset, data)

        package_handle = _parse_package_handle(d.pop("package_handle", UNSET))


        def _parse_package_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        package_sha256 = _parse_package_sha256(d.pop("package_sha256", UNSET))


        def _parse_prebuilt_image(data: object) -> None | PrebuiltImage | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                prebuilt_image_type_0 = PrebuiltImage.from_dict(data)



                return prebuilt_image_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PrebuiltImage | Unset, data)

        prebuilt_image = _parse_prebuilt_image(d.pop("prebuilt_image", UNSET))


        def _parse_publication_commit(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        publication_commit = _parse_publication_commit(d.pop("publication_commit", UNSET))


        def _parse_release_released_at(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        release_released_at = _parse_release_released_at(d.pop("release_released_at", UNSET))


        def _parse_release_version(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        release_version = _parse_release_version(d.pop("release_version", UNSET))


        def _parse_source_bundle_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        source_bundle_sha256 = _parse_source_bundle_sha256(d.pop("source_bundle_sha256", UNSET))


        def _parse_source_path(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        source_path = _parse_source_path(d.pop("source_path", UNSET))


        recipe_revision_projection = cls(
            description=description,
            runtime_engine=runtime_engine,
            tags=tags,
            title=title,
            topology=topology,
            artifact_inputs=artifact_inputs,
            build_model_artifacts=build_model_artifacts,
            build_options=build_options,
            build_resources=build_resources,
            build_security=build_security,
            failure_reason=failure_reason,
            package_handle=package_handle,
            package_sha256=package_sha256,
            prebuilt_image=prebuilt_image,
            publication_commit=publication_commit,
            release_released_at=release_released_at,
            release_version=release_version,
            source_bundle_sha256=source_bundle_sha256,
            source_path=source_path,
        )

        return recipe_revision_projection
