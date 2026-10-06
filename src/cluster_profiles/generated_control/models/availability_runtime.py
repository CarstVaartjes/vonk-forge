from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.availability_runtime_placement_environment_type_0 import AvailabilityRuntimePlacementEnvironmentType0
  from ..models.runtime_argument import RuntimeArgument
  from ..models.runtime_environment_entry import RuntimeEnvironmentEntry
  from ..models.runtime_telemetry_projection import RuntimeTelemetryProjection
  from ..models.writable_path import WritablePath





T = TypeVar("T", bound="AvailabilityRuntime")



@_attrs_define
class AvailabilityRuntime:
    """ The runtime projection an availability operation prepares an image for.

    The compiled adapter fields appear once the runtime is resolved; an
    operation that only reuses a cached image carries the identity subset.

        Attributes:
            architecture (str):
            interface (str):
            adapter (None | str | Unset):
            adapter_version (int | None | Unset):
            arguments (list[RuntimeArgument] | None | Unset):
            build_input_sha256 (None | str | Unset):
            builder_node_id (None | str | Unset):
            entrypoint (list[str] | None | Unset):
            environment (list[RuntimeEnvironmentEntry] | None | Unset):
            image (None | str | Unset):
            image_bytes (int | None | Unset):
            input_intent_sha256 (None | str | Unset):
            placement_environment (AvailabilityRuntimePlacementEnvironmentType0 | None | Unset):
            recipe_revision_id (None | str | Unset):
            telemetry (None | RuntimeTelemetryProjection | Unset):
            writable_paths (list[WritablePath] | None | Unset):
     """

    architecture: str
    interface: str
    adapter: None | str | Unset = UNSET
    adapter_version: int | None | Unset = UNSET
    arguments: list[RuntimeArgument] | None | Unset = UNSET
    build_input_sha256: None | str | Unset = UNSET
    builder_node_id: None | str | Unset = UNSET
    entrypoint: list[str] | None | Unset = UNSET
    environment: list[RuntimeEnvironmentEntry] | None | Unset = UNSET
    image: None | str | Unset = UNSET
    image_bytes: int | None | Unset = UNSET
    input_intent_sha256: None | str | Unset = UNSET
    placement_environment: AvailabilityRuntimePlacementEnvironmentType0 | None | Unset = UNSET
    recipe_revision_id: None | str | Unset = UNSET
    telemetry: None | RuntimeTelemetryProjection | Unset = UNSET
    writable_paths: list[WritablePath] | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.availability_runtime_placement_environment_type_0 import AvailabilityRuntimePlacementEnvironmentType0 # noqa: PLC0415
        from ..models.runtime_argument import RuntimeArgument # noqa: PLC0415
        from ..models.runtime_environment_entry import RuntimeEnvironmentEntry # noqa: PLC0415
        from ..models.runtime_telemetry_projection import RuntimeTelemetryProjection # noqa: PLC0415
        from ..models.writable_path import WritablePath # noqa: PLC0415
        architecture = self.architecture

        interface = self.interface

        adapter: None | str | Unset
        if isinstance(self.adapter, Unset):
            adapter = UNSET
        else:
            adapter = self.adapter

        adapter_version: int | None | Unset
        if isinstance(self.adapter_version, Unset):
            adapter_version = UNSET
        else:
            adapter_version = self.adapter_version

        arguments: list[dict[str, Any]] | None | Unset
        if isinstance(self.arguments, Unset):
            arguments = UNSET
        elif isinstance(self.arguments, list):
            arguments = []
            for arguments_type_0_item_data in self.arguments:
                arguments_type_0_item = arguments_type_0_item_data.to_dict()
                arguments.append(arguments_type_0_item)


        else:
            arguments = self.arguments

        build_input_sha256: None | str | Unset
        if isinstance(self.build_input_sha256, Unset):
            build_input_sha256 = UNSET
        else:
            build_input_sha256 = self.build_input_sha256

        builder_node_id: None | str | Unset
        if isinstance(self.builder_node_id, Unset):
            builder_node_id = UNSET
        else:
            builder_node_id = self.builder_node_id

        entrypoint: list[str] | None | Unset
        if isinstance(self.entrypoint, Unset):
            entrypoint = UNSET
        elif isinstance(self.entrypoint, list):
            entrypoint = self.entrypoint


        else:
            entrypoint = self.entrypoint

        environment: list[dict[str, Any]] | None | Unset
        if isinstance(self.environment, Unset):
            environment = UNSET
        elif isinstance(self.environment, list):
            environment = []
            for environment_type_0_item_data in self.environment:
                environment_type_0_item = environment_type_0_item_data.to_dict()
                environment.append(environment_type_0_item)


        else:
            environment = self.environment

        image: None | str | Unset
        if isinstance(self.image, Unset):
            image = UNSET
        else:
            image = self.image

        image_bytes: int | None | Unset
        if isinstance(self.image_bytes, Unset):
            image_bytes = UNSET
        else:
            image_bytes = self.image_bytes

        input_intent_sha256: None | str | Unset
        if isinstance(self.input_intent_sha256, Unset):
            input_intent_sha256 = UNSET
        else:
            input_intent_sha256 = self.input_intent_sha256

        placement_environment: dict[str, Any] | None | Unset
        if isinstance(self.placement_environment, Unset):
            placement_environment = UNSET
        elif isinstance(self.placement_environment, AvailabilityRuntimePlacementEnvironmentType0):
            placement_environment = self.placement_environment.to_dict()
        else:
            placement_environment = self.placement_environment

        recipe_revision_id: None | str | Unset
        if isinstance(self.recipe_revision_id, Unset):
            recipe_revision_id = UNSET
        else:
            recipe_revision_id = self.recipe_revision_id

        telemetry: dict[str, Any] | None | Unset
        if isinstance(self.telemetry, Unset):
            telemetry = UNSET
        elif isinstance(self.telemetry, RuntimeTelemetryProjection):
            telemetry = self.telemetry.to_dict()
        else:
            telemetry = self.telemetry

        writable_paths: list[dict[str, Any]] | None | Unset
        if isinstance(self.writable_paths, Unset):
            writable_paths = UNSET
        elif isinstance(self.writable_paths, list):
            writable_paths = []
            for writable_paths_type_0_item_data in self.writable_paths:
                writable_paths_type_0_item = writable_paths_type_0_item_data.to_dict()
                writable_paths.append(writable_paths_type_0_item)


        else:
            writable_paths = self.writable_paths


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "architecture": architecture,
            "interface": interface,
        })
        if adapter is not UNSET:
            field_dict["adapter"] = adapter
        if adapter_version is not UNSET:
            field_dict["adapter_version"] = adapter_version
        if arguments is not UNSET:
            field_dict["arguments"] = arguments
        if build_input_sha256 is not UNSET:
            field_dict["build_input_sha256"] = build_input_sha256
        if builder_node_id is not UNSET:
            field_dict["builder_node_id"] = builder_node_id
        if entrypoint is not UNSET:
            field_dict["entrypoint"] = entrypoint
        if environment is not UNSET:
            field_dict["environment"] = environment
        if image is not UNSET:
            field_dict["image"] = image
        if image_bytes is not UNSET:
            field_dict["image_bytes"] = image_bytes
        if input_intent_sha256 is not UNSET:
            field_dict["input_intent_sha256"] = input_intent_sha256
        if placement_environment is not UNSET:
            field_dict["placement_environment"] = placement_environment
        if recipe_revision_id is not UNSET:
            field_dict["recipe_revision_id"] = recipe_revision_id
        if telemetry is not UNSET:
            field_dict["telemetry"] = telemetry
        if writable_paths is not UNSET:
            field_dict["writable_paths"] = writable_paths

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.availability_runtime_placement_environment_type_0 import AvailabilityRuntimePlacementEnvironmentType0 # noqa: PLC0415
        from ..models.runtime_argument import RuntimeArgument # noqa: PLC0415
        from ..models.runtime_environment_entry import RuntimeEnvironmentEntry # noqa: PLC0415
        from ..models.runtime_telemetry_projection import RuntimeTelemetryProjection # noqa: PLC0415
        from ..models.writable_path import WritablePath # noqa: PLC0415
        d = dict(src_dict)
        architecture = d.pop("architecture")

        interface = d.pop("interface")

        def _parse_adapter(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        adapter = _parse_adapter(d.pop("adapter", UNSET))


        def _parse_adapter_version(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        adapter_version = _parse_adapter_version(d.pop("adapter_version", UNSET))


        def _parse_arguments(data: object) -> list[RuntimeArgument] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                arguments_type_0 = []
                _arguments_type_0 = data
                for arguments_type_0_item_data in (_arguments_type_0):
                    arguments_type_0_item = RuntimeArgument.from_dict(arguments_type_0_item_data)



                    arguments_type_0.append(arguments_type_0_item)

                return arguments_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[RuntimeArgument] | None | Unset, data)

        arguments = _parse_arguments(d.pop("arguments", UNSET))


        def _parse_build_input_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        build_input_sha256 = _parse_build_input_sha256(d.pop("build_input_sha256", UNSET))


        def _parse_builder_node_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        builder_node_id = _parse_builder_node_id(d.pop("builder_node_id", UNSET))


        def _parse_entrypoint(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                entrypoint_type_0 = cast(list[str], data)

                return entrypoint_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        entrypoint = _parse_entrypoint(d.pop("entrypoint", UNSET))


        def _parse_environment(data: object) -> list[RuntimeEnvironmentEntry] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                environment_type_0 = []
                _environment_type_0 = data
                for environment_type_0_item_data in (_environment_type_0):
                    environment_type_0_item = RuntimeEnvironmentEntry.from_dict(environment_type_0_item_data)



                    environment_type_0.append(environment_type_0_item)

                return environment_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[RuntimeEnvironmentEntry] | None | Unset, data)

        environment = _parse_environment(d.pop("environment", UNSET))


        def _parse_image(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        image = _parse_image(d.pop("image", UNSET))


        def _parse_image_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        image_bytes = _parse_image_bytes(d.pop("image_bytes", UNSET))


        def _parse_input_intent_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        input_intent_sha256 = _parse_input_intent_sha256(d.pop("input_intent_sha256", UNSET))


        def _parse_placement_environment(data: object) -> AvailabilityRuntimePlacementEnvironmentType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                placement_environment_type_0 = AvailabilityRuntimePlacementEnvironmentType0.from_dict(data)



                return placement_environment_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AvailabilityRuntimePlacementEnvironmentType0 | None | Unset, data)

        placement_environment = _parse_placement_environment(d.pop("placement_environment", UNSET))


        def _parse_recipe_revision_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        recipe_revision_id = _parse_recipe_revision_id(d.pop("recipe_revision_id", UNSET))


        def _parse_telemetry(data: object) -> None | RuntimeTelemetryProjection | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                telemetry_type_0 = RuntimeTelemetryProjection.from_dict(data)



                return telemetry_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RuntimeTelemetryProjection | Unset, data)

        telemetry = _parse_telemetry(d.pop("telemetry", UNSET))


        def _parse_writable_paths(data: object) -> list[WritablePath] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                writable_paths_type_0 = []
                _writable_paths_type_0 = data
                for writable_paths_type_0_item_data in (_writable_paths_type_0):
                    writable_paths_type_0_item = WritablePath.from_dict(writable_paths_type_0_item_data)



                    writable_paths_type_0.append(writable_paths_type_0_item)

                return writable_paths_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[WritablePath] | None | Unset, data)

        writable_paths = _parse_writable_paths(d.pop("writable_paths", UNSET))


        availability_runtime = cls(
            architecture=architecture,
            interface=interface,
            adapter=adapter,
            adapter_version=adapter_version,
            arguments=arguments,
            build_input_sha256=build_input_sha256,
            builder_node_id=builder_node_id,
            entrypoint=entrypoint,
            environment=environment,
            image=image,
            image_bytes=image_bytes,
            input_intent_sha256=input_intent_sha256,
            placement_environment=placement_environment,
            recipe_revision_id=recipe_revision_id,
            telemetry=telemetry,
            writable_paths=writable_paths,
        )

        return availability_runtime
