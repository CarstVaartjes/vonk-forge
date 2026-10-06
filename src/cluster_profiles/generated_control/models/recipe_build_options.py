from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_build_options_format import check_recipe_build_options_format
from ..models.recipe_build_options_format import RecipeBuildOptionsFormat
from ..models.recipe_build_options_layer_compression import check_recipe_build_options_layer_compression
from ..models.recipe_build_options_layer_compression import RecipeBuildOptionsLayerCompression
from ..models.recipe_build_options_squash import check_recipe_build_options_squash
from ..models.recipe_build_options_squash import RecipeBuildOptionsSquash
from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.recipe_build_additional_context import RecipeBuildAdditionalContext
  from ..models.recipe_build_environment_argument import RecipeBuildEnvironmentArgument
  from ..models.recipe_build_metadata import RecipeBuildMetadata





T = TypeVar("T", bound="RecipeBuildOptions")



@_attrs_define
class RecipeBuildOptions:
    """
        Attributes:
            additional_contexts (list[RecipeBuildAdditionalContext]):
            annotations (list[RecipeBuildMetadata]):
            environment (list[RecipeBuildEnvironmentArgument]):
            format_ (RecipeBuildOptionsFormat):
            identity_label (bool):
            jobs (int):
            labels (list[RecipeBuildMetadata]):
            layer_compression (RecipeBuildOptionsLayerCompression):
            layer_labels (list[RecipeBuildMetadata]):
            layers (bool):
            no_hostname (bool):
            no_hosts (bool):
            omit_history (bool):
            os_features (list[str]):
            shm_bytes (int):
            skip_unused_stages (bool):
            squash (RecipeBuildOptionsSquash):
            unset_environment (list[str]):
            unset_labels (list[str]):
            ignorefile (None | str | Unset):
            os_version (None | str | Unset):
            timestamp (int | None | Unset):
     """

    additional_contexts: list[RecipeBuildAdditionalContext]
    annotations: list[RecipeBuildMetadata]
    environment: list[RecipeBuildEnvironmentArgument]
    format_: RecipeBuildOptionsFormat
    identity_label: bool
    jobs: int
    labels: list[RecipeBuildMetadata]
    layer_compression: RecipeBuildOptionsLayerCompression
    layer_labels: list[RecipeBuildMetadata]
    layers: bool
    no_hostname: bool
    no_hosts: bool
    omit_history: bool
    os_features: list[str]
    shm_bytes: int
    skip_unused_stages: bool
    squash: RecipeBuildOptionsSquash
    unset_environment: list[str]
    unset_labels: list[str]
    ignorefile: None | str | Unset = UNSET
    os_version: None | str | Unset = UNSET
    timestamp: int | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_build_additional_context import RecipeBuildAdditionalContext # noqa: PLC0415
        from ..models.recipe_build_environment_argument import RecipeBuildEnvironmentArgument # noqa: PLC0415
        from ..models.recipe_build_metadata import RecipeBuildMetadata # noqa: PLC0415
        additional_contexts = []
        for additional_contexts_item_data in self.additional_contexts:
            additional_contexts_item = additional_contexts_item_data.to_dict()
            additional_contexts.append(additional_contexts_item)



        annotations = []
        for annotations_item_data in self.annotations:
            annotations_item = annotations_item_data.to_dict()
            annotations.append(annotations_item)



        environment = []
        for environment_item_data in self.environment:
            environment_item = environment_item_data.to_dict()
            environment.append(environment_item)



        format_: str = self.format_

        identity_label = self.identity_label

        jobs = self.jobs

        labels = []
        for labels_item_data in self.labels:
            labels_item = labels_item_data.to_dict()
            labels.append(labels_item)



        layer_compression: str = self.layer_compression

        layer_labels = []
        for layer_labels_item_data in self.layer_labels:
            layer_labels_item = layer_labels_item_data.to_dict()
            layer_labels.append(layer_labels_item)



        layers = self.layers

        no_hostname = self.no_hostname

        no_hosts = self.no_hosts

        omit_history = self.omit_history

        os_features = self.os_features



        shm_bytes = self.shm_bytes

        skip_unused_stages = self.skip_unused_stages

        squash: str = self.squash

        unset_environment = self.unset_environment



        unset_labels = self.unset_labels



        ignorefile: None | str | Unset
        if isinstance(self.ignorefile, Unset):
            ignorefile = UNSET
        else:
            ignorefile = self.ignorefile

        os_version: None | str | Unset
        if isinstance(self.os_version, Unset):
            os_version = UNSET
        else:
            os_version = self.os_version

        timestamp: int | None | Unset
        if isinstance(self.timestamp, Unset):
            timestamp = UNSET
        else:
            timestamp = self.timestamp


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "additional_contexts": additional_contexts,
            "annotations": annotations,
            "environment": environment,
            "format": format_,
            "identity_label": identity_label,
            "jobs": jobs,
            "labels": labels,
            "layer_compression": layer_compression,
            "layer_labels": layer_labels,
            "layers": layers,
            "no_hostname": no_hostname,
            "no_hosts": no_hosts,
            "omit_history": omit_history,
            "os_features": os_features,
            "shm_bytes": shm_bytes,
            "skip_unused_stages": skip_unused_stages,
            "squash": squash,
            "unset_environment": unset_environment,
            "unset_labels": unset_labels,
        })
        if ignorefile is not UNSET:
            field_dict["ignorefile"] = ignorefile
        if os_version is not UNSET:
            field_dict["os_version"] = os_version
        if timestamp is not UNSET:
            field_dict["timestamp"] = timestamp

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_build_additional_context import RecipeBuildAdditionalContext # noqa: PLC0415
        from ..models.recipe_build_environment_argument import RecipeBuildEnvironmentArgument # noqa: PLC0415
        from ..models.recipe_build_metadata import RecipeBuildMetadata # noqa: PLC0415
        d = dict(src_dict)
        additional_contexts = []
        _additional_contexts = d.pop("additional_contexts")
        for additional_contexts_item_data in (_additional_contexts):
            additional_contexts_item = RecipeBuildAdditionalContext.from_dict(additional_contexts_item_data)



            additional_contexts.append(additional_contexts_item)


        annotations = []
        _annotations = d.pop("annotations")
        for annotations_item_data in (_annotations):
            annotations_item = RecipeBuildMetadata.from_dict(annotations_item_data)



            annotations.append(annotations_item)


        environment = []
        _environment = d.pop("environment")
        for environment_item_data in (_environment):
            environment_item = RecipeBuildEnvironmentArgument.from_dict(environment_item_data)



            environment.append(environment_item)


        format_ = check_recipe_build_options_format(d.pop("format"))




        identity_label = d.pop("identity_label")

        jobs = d.pop("jobs")

        labels = []
        _labels = d.pop("labels")
        for labels_item_data in (_labels):
            labels_item = RecipeBuildMetadata.from_dict(labels_item_data)



            labels.append(labels_item)


        layer_compression = check_recipe_build_options_layer_compression(d.pop("layer_compression"))




        layer_labels = []
        _layer_labels = d.pop("layer_labels")
        for layer_labels_item_data in (_layer_labels):
            layer_labels_item = RecipeBuildMetadata.from_dict(layer_labels_item_data)



            layer_labels.append(layer_labels_item)


        layers = d.pop("layers")

        no_hostname = d.pop("no_hostname")

        no_hosts = d.pop("no_hosts")

        omit_history = d.pop("omit_history")

        os_features = cast(list[str], d.pop("os_features"))


        shm_bytes = d.pop("shm_bytes")

        skip_unused_stages = d.pop("skip_unused_stages")

        squash = check_recipe_build_options_squash(d.pop("squash"))




        unset_environment = cast(list[str], d.pop("unset_environment"))


        unset_labels = cast(list[str], d.pop("unset_labels"))


        def _parse_ignorefile(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        ignorefile = _parse_ignorefile(d.pop("ignorefile", UNSET))


        def _parse_os_version(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        os_version = _parse_os_version(d.pop("os_version", UNSET))


        def _parse_timestamp(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        timestamp = _parse_timestamp(d.pop("timestamp", UNSET))


        recipe_build_options = cls(
            additional_contexts=additional_contexts,
            annotations=annotations,
            environment=environment,
            format_=format_,
            identity_label=identity_label,
            jobs=jobs,
            labels=labels,
            layer_compression=layer_compression,
            layer_labels=layer_labels,
            layers=layers,
            no_hostname=no_hostname,
            no_hosts=no_hosts,
            omit_history=omit_history,
            os_features=os_features,
            shm_bytes=shm_bytes,
            skip_unused_stages=skip_unused_stages,
            squash=squash,
            unset_environment=unset_environment,
            unset_labels=unset_labels,
            ignorefile=ignorefile,
            os_version=os_version,
            timestamp=timestamp,
        )

        return recipe_build_options
