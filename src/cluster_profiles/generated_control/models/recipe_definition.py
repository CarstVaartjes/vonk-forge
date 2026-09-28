from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.recipe_build_execution import RecipeBuildExecution
  from ..models.recipe_embedding_settings import RecipeEmbeddingSettings
  from ..models.recipe_generation_settings import RecipeGenerationSettings
  from ..models.recipe_identity import RecipeIdentity
  from ..models.recipe_image_execution import RecipeImageExecution
  from ..models.recipe_job_interface import RecipeJobInterface
  from ..models.recipe_job_settings import RecipeJobSettings
  from ..models.recipe_metadata import RecipeMetadata
  from ..models.recipe_model_selection import RecipeModelSelection
  from ..models.recipe_open_ai_interface import RecipeOpenAIInterface
  from ..models.recipe_provenance import RecipeProvenance
  from ..models.recipe_release import RecipeRelease
  from ..models.recipe_runtime import RecipeRuntime
  from ..models.recipe_topology import RecipeTopology
  from ..models.recipe_validation import RecipeValidation





T = TypeVar("T", bound="RecipeDefinition")



@_attrs_define
class RecipeDefinition:
    """ The sole public recipe authoring contract.

        Attributes:
            execution (RecipeBuildExecution | RecipeImageExecution):
            identity (RecipeIdentity):
            interfaces (list[RecipeJobInterface | RecipeOpenAIInterface]):
            metadata (RecipeMetadata):
            models (list[RecipeModelSelection]):
            provenance (RecipeProvenance):
            release (RecipeRelease):
            runtime (RecipeRuntime):
            settings (RecipeEmbeddingSettings | RecipeGenerationSettings | RecipeJobSettings):
            topology (RecipeTopology):
            validation (RecipeValidation):
            kind (Literal['recipe'] | Unset):  Default: 'recipe'.
            schema_version (Literal[2] | Unset):  Default: 2.
     """

    execution: RecipeBuildExecution | RecipeImageExecution
    identity: RecipeIdentity
    interfaces: list[RecipeJobInterface | RecipeOpenAIInterface]
    metadata: RecipeMetadata
    models: list[RecipeModelSelection]
    provenance: RecipeProvenance
    release: RecipeRelease
    runtime: RecipeRuntime
    settings: RecipeEmbeddingSettings | RecipeGenerationSettings | RecipeJobSettings
    topology: RecipeTopology
    validation: RecipeValidation
    kind: Literal['recipe'] | Unset = 'recipe'
    schema_version: Literal[2] | Unset = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_build_execution import RecipeBuildExecution # noqa: PLC0415
        from ..models.recipe_embedding_settings import RecipeEmbeddingSettings # noqa: PLC0415
        from ..models.recipe_generation_settings import RecipeGenerationSettings # noqa: PLC0415
        from ..models.recipe_identity import RecipeIdentity # noqa: PLC0415
        from ..models.recipe_image_execution import RecipeImageExecution # noqa: PLC0415
        from ..models.recipe_job_interface import RecipeJobInterface # noqa: PLC0415
        from ..models.recipe_job_settings import RecipeJobSettings # noqa: PLC0415
        from ..models.recipe_metadata import RecipeMetadata # noqa: PLC0415
        from ..models.recipe_model_selection import RecipeModelSelection # noqa: PLC0415
        from ..models.recipe_open_ai_interface import RecipeOpenAIInterface # noqa: PLC0415
        from ..models.recipe_provenance import RecipeProvenance # noqa: PLC0415
        from ..models.recipe_release import RecipeRelease # noqa: PLC0415
        from ..models.recipe_runtime import RecipeRuntime # noqa: PLC0415
        from ..models.recipe_topology import RecipeTopology # noqa: PLC0415
        from ..models.recipe_validation import RecipeValidation # noqa: PLC0415
        execution: dict[str, Any]
        if isinstance(self.execution, RecipeImageExecution):
            execution = self.execution.to_dict()
        else:
            execution = self.execution.to_dict()


        identity = self.identity.to_dict()

        interfaces = []
        for interfaces_item_data in self.interfaces:
            interfaces_item: dict[str, Any]
            if isinstance(interfaces_item_data, RecipeOpenAIInterface):
                interfaces_item = interfaces_item_data.to_dict()
            else:
                interfaces_item = interfaces_item_data.to_dict()

            interfaces.append(interfaces_item)



        metadata = self.metadata.to_dict()

        models = []
        for models_item_data in self.models:
            models_item = models_item_data.to_dict()
            models.append(models_item)



        provenance = self.provenance.to_dict()

        release = self.release.to_dict()

        runtime = self.runtime.to_dict()

        settings: dict[str, Any]
        if isinstance(self.settings, RecipeGenerationSettings):
            settings = self.settings.to_dict()
        elif isinstance(self.settings, RecipeEmbeddingSettings):
            settings = self.settings.to_dict()
        else:
            settings = self.settings.to_dict()


        topology = self.topology.to_dict()

        validation = self.validation.to_dict()

        kind = self.kind

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "execution": execution,
            "identity": identity,
            "interfaces": interfaces,
            "metadata": metadata,
            "models": models,
            "provenance": provenance,
            "release": release,
            "runtime": runtime,
            "settings": settings,
            "topology": topology,
            "validation": validation,
        })
        if kind is not UNSET:
            field_dict["kind"] = kind
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_build_execution import RecipeBuildExecution # noqa: PLC0415
        from ..models.recipe_embedding_settings import RecipeEmbeddingSettings # noqa: PLC0415
        from ..models.recipe_generation_settings import RecipeGenerationSettings # noqa: PLC0415
        from ..models.recipe_identity import RecipeIdentity # noqa: PLC0415
        from ..models.recipe_image_execution import RecipeImageExecution # noqa: PLC0415
        from ..models.recipe_job_interface import RecipeJobInterface # noqa: PLC0415
        from ..models.recipe_job_settings import RecipeJobSettings # noqa: PLC0415
        from ..models.recipe_metadata import RecipeMetadata # noqa: PLC0415
        from ..models.recipe_model_selection import RecipeModelSelection # noqa: PLC0415
        from ..models.recipe_open_ai_interface import RecipeOpenAIInterface # noqa: PLC0415
        from ..models.recipe_provenance import RecipeProvenance # noqa: PLC0415
        from ..models.recipe_release import RecipeRelease # noqa: PLC0415
        from ..models.recipe_runtime import RecipeRuntime # noqa: PLC0415
        from ..models.recipe_topology import RecipeTopology # noqa: PLC0415
        from ..models.recipe_validation import RecipeValidation # noqa: PLC0415
        d = dict(src_dict)
        def _parse_execution(data: object) -> RecipeBuildExecution | RecipeImageExecution:
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                execution_type_0 = RecipeImageExecution.from_dict(data)



                return execution_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            if not isinstance(data, dict):
                raise TypeError()
            execution_type_1 = RecipeBuildExecution.from_dict(data)



            return execution_type_1

        execution = _parse_execution(d.pop("execution"))


        identity = RecipeIdentity.from_dict(d.pop("identity"))




        interfaces = []
        _interfaces = d.pop("interfaces")
        for interfaces_item_data in (_interfaces):
            def _parse_interfaces_item(data: object) -> RecipeJobInterface | RecipeOpenAIInterface:
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    interfaces_item_type_0 = RecipeOpenAIInterface.from_dict(data)



                    return interfaces_item_type_0
                except (TypeError, ValueError, AttributeError, KeyError):
                    pass
                if not isinstance(data, dict):
                    raise TypeError()
                interfaces_item_type_1 = RecipeJobInterface.from_dict(data)



                return interfaces_item_type_1

            interfaces_item = _parse_interfaces_item(interfaces_item_data)

            interfaces.append(interfaces_item)


        metadata = RecipeMetadata.from_dict(d.pop("metadata"))




        models = []
        _models = d.pop("models")
        for models_item_data in (_models):
            models_item = RecipeModelSelection.from_dict(models_item_data)



            models.append(models_item)


        provenance = RecipeProvenance.from_dict(d.pop("provenance"))




        release = RecipeRelease.from_dict(d.pop("release"))




        runtime = RecipeRuntime.from_dict(d.pop("runtime"))




        def _parse_settings(data: object) -> RecipeEmbeddingSettings | RecipeGenerationSettings | RecipeJobSettings:
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                settings_type_0 = RecipeGenerationSettings.from_dict(data)



                return settings_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                settings_type_1 = RecipeEmbeddingSettings.from_dict(data)



                return settings_type_1
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            if not isinstance(data, dict):
                raise TypeError()
            settings_type_2 = RecipeJobSettings.from_dict(data)



            return settings_type_2

        settings = _parse_settings(d.pop("settings"))


        topology = RecipeTopology.from_dict(d.pop("topology"))




        validation = RecipeValidation.from_dict(d.pop("validation"))




        kind = cast(Literal['recipe'] | Unset , d.pop("kind", UNSET))
        if kind != 'recipe' and not isinstance(kind, Unset):
            raise ValueError(f"kind must match const 'recipe', got '{kind}'")

        schema_version = cast(Literal[2] | Unset , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        recipe_definition = cls(
            execution=execution,
            identity=identity,
            interfaces=interfaces,
            metadata=metadata,
            models=models,
            provenance=provenance,
            release=release,
            runtime=runtime,
            settings=settings,
            topology=topology,
            validation=validation,
            kind=kind,
            schema_version=schema_version,
        )

        return recipe_definition
