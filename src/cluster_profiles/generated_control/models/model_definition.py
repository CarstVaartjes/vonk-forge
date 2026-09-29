from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.model_definition_modalities_item import check_model_definition_modalities_item
from ..models.model_definition_modalities_item import ModelDefinitionModalitiesItem
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.git_hub_release_source import GitHubReleaseSource
  from ..models.model_file import ModelFile
  from ..models.model_format import ModelFormat
  from ..models.model_identity import ModelIdentity
  from ..models.model_license import ModelLicense
  from ..models.model_metadata import ModelMetadata
  from ..models.model_reference import ModelReference
  from ..models.model_source import ModelSource





T = TypeVar("T", bound="ModelDefinition")



@_attrs_define
class ModelDefinition:
    """ One exact model version and variant, including its complete manifest.

        Attributes:
            capabilities (list[str]):
            dependencies (list[ModelReference]):
            files (list[ModelFile]):
            format_ (ModelFormat):
            identity (ModelIdentity): The family, logical model, exact version, and selected variant.
            license_ (ModelLicense):
            metadata (ModelMetadata):
            modalities (list[ModelDefinitionModalitiesItem]):
            requires_token (bool):
            source (GitHubReleaseSource | ModelSource):
            kind (Literal['model'] | Unset):  Default: 'model'.
     """

    capabilities: list[str]
    dependencies: list[ModelReference]
    files: list[ModelFile]
    format_: ModelFormat
    identity: ModelIdentity
    license_: ModelLicense
    metadata: ModelMetadata
    modalities: list[ModelDefinitionModalitiesItem]
    requires_token: bool
    source: GitHubReleaseSource | ModelSource
    kind: Literal['model'] | Unset = 'model'





    def to_dict(self) -> dict[str, Any]:
        from ..models.git_hub_release_source import GitHubReleaseSource # noqa: PLC0415
        from ..models.model_file import ModelFile # noqa: PLC0415
        from ..models.model_format import ModelFormat # noqa: PLC0415
        from ..models.model_identity import ModelIdentity # noqa: PLC0415
        from ..models.model_license import ModelLicense # noqa: PLC0415
        from ..models.model_metadata import ModelMetadata # noqa: PLC0415
        from ..models.model_reference import ModelReference # noqa: PLC0415
        from ..models.model_source import ModelSource # noqa: PLC0415
        capabilities = self.capabilities



        dependencies = []
        for dependencies_item_data in self.dependencies:
            dependencies_item = dependencies_item_data.to_dict()
            dependencies.append(dependencies_item)



        files = []
        for files_item_data in self.files:
            files_item = files_item_data.to_dict()
            files.append(files_item)



        format_ = self.format_.to_dict()

        identity = self.identity.to_dict()

        license_ = self.license_.to_dict()

        metadata = self.metadata.to_dict()

        modalities = []
        for modalities_item_data in self.modalities:
            modalities_item: str = modalities_item_data
            modalities.append(modalities_item)



        requires_token = self.requires_token

        source: dict[str, Any]
        if isinstance(self.source, ModelSource):
            source = self.source.to_dict()
        else:
            source = self.source.to_dict()


        kind = self.kind


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "capabilities": capabilities,
            "dependencies": dependencies,
            "files": files,
            "format": format_,
            "identity": identity,
            "license": license_,
            "metadata": metadata,
            "modalities": modalities,
            "requires_token": requires_token,
            "source": source,
        })
        if kind is not UNSET:
            field_dict["kind"] = kind

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.git_hub_release_source import GitHubReleaseSource # noqa: PLC0415
        from ..models.model_file import ModelFile # noqa: PLC0415
        from ..models.model_format import ModelFormat # noqa: PLC0415
        from ..models.model_identity import ModelIdentity # noqa: PLC0415
        from ..models.model_license import ModelLicense # noqa: PLC0415
        from ..models.model_metadata import ModelMetadata # noqa: PLC0415
        from ..models.model_reference import ModelReference # noqa: PLC0415
        from ..models.model_source import ModelSource # noqa: PLC0415
        d = dict(src_dict)
        capabilities = cast(list[str], d.pop("capabilities"))


        dependencies = []
        _dependencies = d.pop("dependencies")
        for dependencies_item_data in (_dependencies):
            dependencies_item = ModelReference.from_dict(dependencies_item_data)



            dependencies.append(dependencies_item)


        files = []
        _files = d.pop("files")
        for files_item_data in (_files):
            files_item = ModelFile.from_dict(files_item_data)



            files.append(files_item)


        format_ = ModelFormat.from_dict(d.pop("format"))




        identity = ModelIdentity.from_dict(d.pop("identity"))




        license_ = ModelLicense.from_dict(d.pop("license"))




        metadata = ModelMetadata.from_dict(d.pop("metadata"))




        modalities = []
        _modalities = d.pop("modalities")
        for modalities_item_data in (_modalities):
            modalities_item = check_model_definition_modalities_item(modalities_item_data)



            modalities.append(modalities_item)


        requires_token = d.pop("requires_token")

        def _parse_source(data: object) -> GitHubReleaseSource | ModelSource:
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                source_type_0 = ModelSource.from_dict(data)



                return source_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            if not isinstance(data, dict):
                raise TypeError()
            source_type_1 = GitHubReleaseSource.from_dict(data)



            return source_type_1

        source = _parse_source(d.pop("source"))


        kind = cast(Literal['model'] | Unset , d.pop("kind", UNSET))
        if kind != 'model' and not isinstance(kind, Unset):
            raise ValueError(f"kind must match const 'model', got '{kind}'")

        model_definition = cls(
            capabilities=capabilities,
            dependencies=dependencies,
            files=files,
            format_=format_,
            identity=identity,
            license_=license_,
            metadata=metadata,
            modalities=modalities,
            requires_token=requires_token,
            source=source,
            kind=kind,
        )

        return model_definition
