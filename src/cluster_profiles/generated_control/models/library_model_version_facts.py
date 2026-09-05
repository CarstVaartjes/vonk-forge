from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.library_model_version_facts_availability_type_0 import check_library_model_version_facts_availability_type_0
from ..models.library_model_version_facts_availability_type_0 import LibraryModelVersionFactsAvailabilityType0
from ..models.library_model_version_facts_state import check_library_model_version_facts_state
from ..models.library_model_version_facts_state import LibraryModelVersionFactsState
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.library_model_metadata import LibraryModelMetadata
  from ..models.library_model_source import LibraryModelSource
  from ..models.library_model_sizes import LibraryModelSizes
  from ..models.library_catalog_reference import LibraryCatalogReference
  from ..models.library_model_lineage import LibraryModelLineage
  from ..models.library_model_artifact import LibraryModelArtifact
  from ..models.library_model_parameters import LibraryModelParameters
  from ..models.library_model_format import LibraryModelFormat
  from ..models.library_model_limits import LibraryModelLimits
  from ..models.library_model_definition import LibraryModelDefinition
  from ..models.library_model_family import LibraryModelFamily
  from ..models.library_projection_reason import LibraryProjectionReason
  from ..models.model_version_identity import ModelVersionIdentity





T = TypeVar("T", bound="LibraryModelVersionFacts")



@_attrs_define
class LibraryModelVersionFacts:
    """ Exact schema-valid model-version facts, with unknown fields fail-closed.

        Attributes:
            artifacts (list['LibraryModelArtifact']):
            dependencies (list['LibraryCatalogReference']):
            identity (ModelVersionIdentity):
            state (LibraryModelVersionFactsState):
            availability (Union[LibraryModelVersionFactsAvailabilityType0, None, Unset]):
            family (Union['LibraryModelFamily', None, Unset]):
            format_ (Union['LibraryModelFormat', None, Unset]):
            limits (Union['LibraryModelLimits', None, Unset]):
            lineage (Union['LibraryModelLineage', None, Unset]):
            metadata (Union['LibraryModelMetadata', None, Unset]):
            model (Union['LibraryCatalogReference', None, Unset]):
            model_definition (Union['LibraryModelDefinition', None, Unset]):
            parameters (Union['LibraryModelParameters', None, Unset]):
            reasons (Union[Unset, list['LibraryProjectionReason']]):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
            sizes (Union['LibraryModelSizes', None, Unset]):
            source (Union['LibraryModelSource', None, Unset]):
            version (Union[None, Unset, str]):
     """

    artifacts: list['LibraryModelArtifact']
    dependencies: list['LibraryCatalogReference']
    identity: 'ModelVersionIdentity'
    state: LibraryModelVersionFactsState
    availability: Union[LibraryModelVersionFactsAvailabilityType0, None, Unset] = UNSET
    family: Union['LibraryModelFamily', None, Unset] = UNSET
    format_: Union['LibraryModelFormat', None, Unset] = UNSET
    limits: Union['LibraryModelLimits', None, Unset] = UNSET
    lineage: Union['LibraryModelLineage', None, Unset] = UNSET
    metadata: Union['LibraryModelMetadata', None, Unset] = UNSET
    model: Union['LibraryCatalogReference', None, Unset] = UNSET
    model_definition: Union['LibraryModelDefinition', None, Unset] = UNSET
    parameters: Union['LibraryModelParameters', None, Unset] = UNSET
    reasons: Union[Unset, list['LibraryProjectionReason']] = UNSET
    schema_version: Union[Literal[2], Unset] = 2
    sizes: Union['LibraryModelSizes', None, Unset] = UNSET
    source: Union['LibraryModelSource', None, Unset] = UNSET
    version: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.library_model_metadata import LibraryModelMetadata
        from ..models.library_model_source import LibraryModelSource
        from ..models.library_model_sizes import LibraryModelSizes
        from ..models.library_catalog_reference import LibraryCatalogReference
        from ..models.library_model_lineage import LibraryModelLineage
        from ..models.library_model_artifact import LibraryModelArtifact
        from ..models.library_model_parameters import LibraryModelParameters
        from ..models.library_model_format import LibraryModelFormat
        from ..models.library_model_limits import LibraryModelLimits
        from ..models.library_model_definition import LibraryModelDefinition
        from ..models.library_model_family import LibraryModelFamily
        from ..models.library_projection_reason import LibraryProjectionReason
        from ..models.model_version_identity import ModelVersionIdentity
        artifacts = []
        for artifacts_item_data in self.artifacts:
            artifacts_item = artifacts_item_data.to_dict()
            artifacts.append(artifacts_item)



        dependencies = []
        for dependencies_item_data in self.dependencies:
            dependencies_item = dependencies_item_data.to_dict()
            dependencies.append(dependencies_item)



        identity = self.identity.to_dict()

        state: str = self.state

        availability: Union[None, Unset, str]
        if isinstance(self.availability, Unset):
            availability = UNSET
        elif isinstance(self.availability, str):
            availability = self.availability
        else:
            availability = self.availability

        family: Union[None, Unset, dict[str, Any]]
        if isinstance(self.family, Unset):
            family = UNSET
        elif isinstance(self.family, LibraryModelFamily):
            family = self.family.to_dict()
        else:
            family = self.family

        format_: Union[None, Unset, dict[str, Any]]
        if isinstance(self.format_, Unset):
            format_ = UNSET
        elif isinstance(self.format_, LibraryModelFormat):
            format_ = self.format_.to_dict()
        else:
            format_ = self.format_

        limits: Union[None, Unset, dict[str, Any]]
        if isinstance(self.limits, Unset):
            limits = UNSET
        elif isinstance(self.limits, LibraryModelLimits):
            limits = self.limits.to_dict()
        else:
            limits = self.limits

        lineage: Union[None, Unset, dict[str, Any]]
        if isinstance(self.lineage, Unset):
            lineage = UNSET
        elif isinstance(self.lineage, LibraryModelLineage):
            lineage = self.lineage.to_dict()
        else:
            lineage = self.lineage

        metadata: Union[None, Unset, dict[str, Any]]
        if isinstance(self.metadata, Unset):
            metadata = UNSET
        elif isinstance(self.metadata, LibraryModelMetadata):
            metadata = self.metadata.to_dict()
        else:
            metadata = self.metadata

        model: Union[None, Unset, dict[str, Any]]
        if isinstance(self.model, Unset):
            model = UNSET
        elif isinstance(self.model, LibraryCatalogReference):
            model = self.model.to_dict()
        else:
            model = self.model

        model_definition: Union[None, Unset, dict[str, Any]]
        if isinstance(self.model_definition, Unset):
            model_definition = UNSET
        elif isinstance(self.model_definition, LibraryModelDefinition):
            model_definition = self.model_definition.to_dict()
        else:
            model_definition = self.model_definition

        parameters: Union[None, Unset, dict[str, Any]]
        if isinstance(self.parameters, Unset):
            parameters = UNSET
        elif isinstance(self.parameters, LibraryModelParameters):
            parameters = self.parameters.to_dict()
        else:
            parameters = self.parameters

        reasons: Union[Unset, list[dict[str, Any]]] = UNSET
        if not isinstance(self.reasons, Unset):
            reasons = []
            for reasons_item_data in self.reasons:
                reasons_item = reasons_item_data.to_dict()
                reasons.append(reasons_item)



        schema_version = self.schema_version

        sizes: Union[None, Unset, dict[str, Any]]
        if isinstance(self.sizes, Unset):
            sizes = UNSET
        elif isinstance(self.sizes, LibraryModelSizes):
            sizes = self.sizes.to_dict()
        else:
            sizes = self.sizes

        source: Union[None, Unset, dict[str, Any]]
        if isinstance(self.source, Unset):
            source = UNSET
        elif isinstance(self.source, LibraryModelSource):
            source = self.source.to_dict()
        else:
            source = self.source

        version: Union[None, Unset, str]
        if isinstance(self.version, Unset):
            version = UNSET
        else:
            version = self.version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifacts": artifacts,
            "dependencies": dependencies,
            "identity": identity,
            "state": state,
        })
        if availability is not UNSET:
            field_dict["availability"] = availability
        if family is not UNSET:
            field_dict["family"] = family
        if format_ is not UNSET:
            field_dict["format"] = format_
        if limits is not UNSET:
            field_dict["limits"] = limits
        if lineage is not UNSET:
            field_dict["lineage"] = lineage
        if metadata is not UNSET:
            field_dict["metadata"] = metadata
        if model is not UNSET:
            field_dict["model"] = model
        if model_definition is not UNSET:
            field_dict["model_definition"] = model_definition
        if parameters is not UNSET:
            field_dict["parameters"] = parameters
        if reasons is not UNSET:
            field_dict["reasons"] = reasons
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if sizes is not UNSET:
            field_dict["sizes"] = sizes
        if source is not UNSET:
            field_dict["source"] = source
        if version is not UNSET:
            field_dict["version"] = version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.library_model_metadata import LibraryModelMetadata
        from ..models.library_model_source import LibraryModelSource
        from ..models.library_model_sizes import LibraryModelSizes
        from ..models.library_catalog_reference import LibraryCatalogReference
        from ..models.library_model_lineage import LibraryModelLineage
        from ..models.library_model_artifact import LibraryModelArtifact
        from ..models.library_model_parameters import LibraryModelParameters
        from ..models.library_model_format import LibraryModelFormat
        from ..models.library_model_limits import LibraryModelLimits
        from ..models.library_model_definition import LibraryModelDefinition
        from ..models.library_model_family import LibraryModelFamily
        from ..models.library_projection_reason import LibraryProjectionReason
        from ..models.model_version_identity import ModelVersionIdentity
        d = dict(src_dict)
        artifacts = []
        _artifacts = d.pop("artifacts")
        for artifacts_item_data in (_artifacts):
            artifacts_item = LibraryModelArtifact.from_dict(artifacts_item_data)



            artifacts.append(artifacts_item)


        dependencies = []
        _dependencies = d.pop("dependencies")
        for dependencies_item_data in (_dependencies):
            dependencies_item = LibraryCatalogReference.from_dict(dependencies_item_data)



            dependencies.append(dependencies_item)


        identity = ModelVersionIdentity.from_dict(d.pop("identity"))




        state = check_library_model_version_facts_state(d.pop("state"))




        def _parse_availability(data: object) -> Union[LibraryModelVersionFactsAvailabilityType0, None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                availability_type_0 = check_library_model_version_facts_availability_type_0(data)



                return availability_type_0
            except: # noqa: E722
                pass
            return cast(Union[LibraryModelVersionFactsAvailabilityType0, None, Unset], data)

        availability = _parse_availability(d.pop("availability", UNSET))


        def _parse_family(data: object) -> Union['LibraryModelFamily', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                family_type_0 = LibraryModelFamily.from_dict(data)



                return family_type_0
            except: # noqa: E722
                pass
            return cast(Union['LibraryModelFamily', None, Unset], data)

        family = _parse_family(d.pop("family", UNSET))


        def _parse_format_(data: object) -> Union['LibraryModelFormat', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                format_type_0 = LibraryModelFormat.from_dict(data)



                return format_type_0
            except: # noqa: E722
                pass
            return cast(Union['LibraryModelFormat', None, Unset], data)

        format_ = _parse_format_(d.pop("format", UNSET))


        def _parse_limits(data: object) -> Union['LibraryModelLimits', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                limits_type_0 = LibraryModelLimits.from_dict(data)



                return limits_type_0
            except: # noqa: E722
                pass
            return cast(Union['LibraryModelLimits', None, Unset], data)

        limits = _parse_limits(d.pop("limits", UNSET))


        def _parse_lineage(data: object) -> Union['LibraryModelLineage', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                lineage_type_0 = LibraryModelLineage.from_dict(data)



                return lineage_type_0
            except: # noqa: E722
                pass
            return cast(Union['LibraryModelLineage', None, Unset], data)

        lineage = _parse_lineage(d.pop("lineage", UNSET))


        def _parse_metadata(data: object) -> Union['LibraryModelMetadata', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                metadata_type_0 = LibraryModelMetadata.from_dict(data)



                return metadata_type_0
            except: # noqa: E722
                pass
            return cast(Union['LibraryModelMetadata', None, Unset], data)

        metadata = _parse_metadata(d.pop("metadata", UNSET))


        def _parse_model(data: object) -> Union['LibraryCatalogReference', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                model_type_0 = LibraryCatalogReference.from_dict(data)



                return model_type_0
            except: # noqa: E722
                pass
            return cast(Union['LibraryCatalogReference', None, Unset], data)

        model = _parse_model(d.pop("model", UNSET))


        def _parse_model_definition(data: object) -> Union['LibraryModelDefinition', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                model_definition_type_0 = LibraryModelDefinition.from_dict(data)



                return model_definition_type_0
            except: # noqa: E722
                pass
            return cast(Union['LibraryModelDefinition', None, Unset], data)

        model_definition = _parse_model_definition(d.pop("model_definition", UNSET))


        def _parse_parameters(data: object) -> Union['LibraryModelParameters', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                parameters_type_0 = LibraryModelParameters.from_dict(data)



                return parameters_type_0
            except: # noqa: E722
                pass
            return cast(Union['LibraryModelParameters', None, Unset], data)

        parameters = _parse_parameters(d.pop("parameters", UNSET))


        reasons = []
        _reasons = d.pop("reasons", UNSET)
        for reasons_item_data in (_reasons or []):
            reasons_item = LibraryProjectionReason.from_dict(reasons_item_data)



            reasons.append(reasons_item)


        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        def _parse_sizes(data: object) -> Union['LibraryModelSizes', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                sizes_type_0 = LibraryModelSizes.from_dict(data)



                return sizes_type_0
            except: # noqa: E722
                pass
            return cast(Union['LibraryModelSizes', None, Unset], data)

        sizes = _parse_sizes(d.pop("sizes", UNSET))


        def _parse_source(data: object) -> Union['LibraryModelSource', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                source_type_0 = LibraryModelSource.from_dict(data)



                return source_type_0
            except: # noqa: E722
                pass
            return cast(Union['LibraryModelSource', None, Unset], data)

        source = _parse_source(d.pop("source", UNSET))


        def _parse_version(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        version = _parse_version(d.pop("version", UNSET))


        library_model_version_facts = cls(
            artifacts=artifacts,
            dependencies=dependencies,
            identity=identity,
            state=state,
            availability=availability,
            family=family,
            format_=format_,
            limits=limits,
            lineage=lineage,
            metadata=metadata,
            model=model,
            model_definition=model_definition,
            parameters=parameters,
            reasons=reasons,
            schema_version=schema_version,
            sizes=sizes,
            source=source,
            version=version,
        )

        return library_model_version_facts
