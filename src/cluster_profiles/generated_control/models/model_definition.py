from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.model_definition_modalities_item import check_model_definition_modalities_item
from ..models.model_definition_modalities_item import ModelDefinitionModalitiesItem
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.model_format import ModelFormat
  from ..models.model_limits import ModelLimits
  from ..models.model_source import ModelSource
  from ..models.model_identity import ModelIdentity
  from ..models.model_reference import ModelReference
  from ..models.model_license import ModelLicense
  from ..models.model_access import ModelAccess
  from ..models.model_lineage import ModelLineage
  from ..models.model_file import ModelFile
  from ..models.model_metadata import ModelMetadata
  from ..models.model_parameters import ModelParameters
  from ..models.model_provenance import ModelProvenance
  from ..models.model_capabilities import ModelCapabilities





T = TypeVar("T", bound="ModelDefinition")



@_attrs_define
class ModelDefinition:
    """ One exact model version and variant, including its complete manifest.

        Attributes:
            access (ModelAccess):
            capabilities (ModelCapabilities):
            dependencies (list['ModelReference']):
            files (list['ModelFile']):
            format_ (ModelFormat):
            identity (ModelIdentity): The family, logical model, exact version, and selected variant.
            license_ (ModelLicense):
            limits (ModelLimits):
            lineage (ModelLineage):
            metadata (ModelMetadata):
            modalities (list[ModelDefinitionModalitiesItem]):
            parameters (ModelParameters):
            provenance (ModelProvenance):
            source (ModelSource):
            kind (Union[Literal['model'], Unset]):  Default: 'model'.
            schema_version (Union[Literal[2], Unset]):  Default: 2.
            supersedes (Union['ModelReference', None, Unset]):
     """

    access: 'ModelAccess'
    capabilities: 'ModelCapabilities'
    dependencies: list['ModelReference']
    files: list['ModelFile']
    format_: 'ModelFormat'
    identity: 'ModelIdentity'
    license_: 'ModelLicense'
    limits: 'ModelLimits'
    lineage: 'ModelLineage'
    metadata: 'ModelMetadata'
    modalities: list[ModelDefinitionModalitiesItem]
    parameters: 'ModelParameters'
    provenance: 'ModelProvenance'
    source: 'ModelSource'
    kind: Union[Literal['model'], Unset] = 'model'
    schema_version: Union[Literal[2], Unset] = 2
    supersedes: Union['ModelReference', None, Unset] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.model_format import ModelFormat
        from ..models.model_limits import ModelLimits
        from ..models.model_source import ModelSource
        from ..models.model_identity import ModelIdentity
        from ..models.model_reference import ModelReference
        from ..models.model_license import ModelLicense
        from ..models.model_access import ModelAccess
        from ..models.model_lineage import ModelLineage
        from ..models.model_file import ModelFile
        from ..models.model_metadata import ModelMetadata
        from ..models.model_parameters import ModelParameters
        from ..models.model_provenance import ModelProvenance
        from ..models.model_capabilities import ModelCapabilities
        access = self.access.to_dict()

        capabilities = self.capabilities.to_dict()

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

        limits = self.limits.to_dict()

        lineage = self.lineage.to_dict()

        metadata = self.metadata.to_dict()

        modalities = []
        for modalities_item_data in self.modalities:
            modalities_item: str = modalities_item_data
            modalities.append(modalities_item)



        parameters = self.parameters.to_dict()

        provenance = self.provenance.to_dict()

        source = self.source.to_dict()

        kind = self.kind

        schema_version = self.schema_version

        supersedes: Union[None, Unset, dict[str, Any]]
        if isinstance(self.supersedes, Unset):
            supersedes = UNSET
        elif isinstance(self.supersedes, ModelReference):
            supersedes = self.supersedes.to_dict()
        else:
            supersedes = self.supersedes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "access": access,
            "capabilities": capabilities,
            "dependencies": dependencies,
            "files": files,
            "format": format_,
            "identity": identity,
            "license": license_,
            "limits": limits,
            "lineage": lineage,
            "metadata": metadata,
            "modalities": modalities,
            "parameters": parameters,
            "provenance": provenance,
            "source": source,
        })
        if kind is not UNSET:
            field_dict["kind"] = kind
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if supersedes is not UNSET:
            field_dict["supersedes"] = supersedes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.model_format import ModelFormat
        from ..models.model_limits import ModelLimits
        from ..models.model_source import ModelSource
        from ..models.model_identity import ModelIdentity
        from ..models.model_reference import ModelReference
        from ..models.model_license import ModelLicense
        from ..models.model_access import ModelAccess
        from ..models.model_lineage import ModelLineage
        from ..models.model_file import ModelFile
        from ..models.model_metadata import ModelMetadata
        from ..models.model_parameters import ModelParameters
        from ..models.model_provenance import ModelProvenance
        from ..models.model_capabilities import ModelCapabilities
        d = dict(src_dict)
        access = ModelAccess.from_dict(d.pop("access"))




        capabilities = ModelCapabilities.from_dict(d.pop("capabilities"))




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




        limits = ModelLimits.from_dict(d.pop("limits"))




        lineage = ModelLineage.from_dict(d.pop("lineage"))




        metadata = ModelMetadata.from_dict(d.pop("metadata"))




        modalities = []
        _modalities = d.pop("modalities")
        for modalities_item_data in (_modalities):
            modalities_item = check_model_definition_modalities_item(modalities_item_data)



            modalities.append(modalities_item)


        parameters = ModelParameters.from_dict(d.pop("parameters"))




        provenance = ModelProvenance.from_dict(d.pop("provenance"))




        source = ModelSource.from_dict(d.pop("source"))




        kind = cast(Union[Literal['model'], Unset] , d.pop("kind", UNSET))
        if kind != 'model' and not isinstance(kind, Unset):
            raise ValueError(f"kind must match const 'model', got '{kind}'")

        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        def _parse_supersedes(data: object) -> Union['ModelReference', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                supersedes_type_0 = ModelReference.from_dict(data)



                return supersedes_type_0
            except: # noqa: E722
                pass
            return cast(Union['ModelReference', None, Unset], data)

        supersedes = _parse_supersedes(d.pop("supersedes", UNSET))


        model_definition = cls(
            access=access,
            capabilities=capabilities,
            dependencies=dependencies,
            files=files,
            format_=format_,
            identity=identity,
            license_=license_,
            limits=limits,
            lineage=lineage,
            metadata=metadata,
            modalities=modalities,
            parameters=parameters,
            provenance=provenance,
            source=source,
            kind=kind,
            schema_version=schema_version,
            supersedes=supersedes,
        )

        return model_definition
