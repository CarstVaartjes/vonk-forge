from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.visual_build_options_format import check_visual_build_options_format
from ..models.visual_build_options_format import VisualBuildOptionsFormat
from ..models.visual_build_options_layer_compression import check_visual_build_options_layer_compression
from ..models.visual_build_options_layer_compression import VisualBuildOptionsLayerCompression
from ..models.visual_build_options_squash import check_visual_build_options_squash
from ..models.visual_build_options_squash import VisualBuildOptionsSquash
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.visual_build_additional_context import VisualBuildAdditionalContext
  from ..models.visual_build_option_value import VisualBuildOptionValue





T = TypeVar("T", bound="VisualBuildOptions")



@_attrs_define
class VisualBuildOptions:
    """
        Attributes:
            additional_contexts (list['VisualBuildAdditionalContext']):
            annotations (list['VisualBuildOptionValue']):
            environment (list['VisualBuildOptionValue']):
            format_ (VisualBuildOptionsFormat):
            identity_label (bool):
            ignorefile (Union[None, str]):
            jobs (int):
            labels (list['VisualBuildOptionValue']):
            layer_compression (VisualBuildOptionsLayerCompression):
            layer_labels (list['VisualBuildOptionValue']):
            layers (bool):
            no_hostname (bool):
            no_hosts (bool):
            omit_history (bool):
            os_features (list[str]):
            os_version (Union[None, str]):
            shm_bytes (int):
            skip_unused_stages (bool):
            squash (VisualBuildOptionsSquash):
            unset_environment (list[str]):
            unset_labels (list[str]):
            timestamp (Union[None, Unset, int]):
     """

    additional_contexts: list['VisualBuildAdditionalContext']
    annotations: list['VisualBuildOptionValue']
    environment: list['VisualBuildOptionValue']
    format_: VisualBuildOptionsFormat
    identity_label: bool
    ignorefile: Union[None, str]
    jobs: int
    labels: list['VisualBuildOptionValue']
    layer_compression: VisualBuildOptionsLayerCompression
    layer_labels: list['VisualBuildOptionValue']
    layers: bool
    no_hostname: bool
    no_hosts: bool
    omit_history: bool
    os_features: list[str]
    os_version: Union[None, str]
    shm_bytes: int
    skip_unused_stages: bool
    squash: VisualBuildOptionsSquash
    unset_environment: list[str]
    unset_labels: list[str]
    timestamp: Union[None, Unset, int] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.visual_build_additional_context import VisualBuildAdditionalContext
        from ..models.visual_build_option_value import VisualBuildOptionValue
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

        ignorefile: Union[None, str]
        ignorefile = self.ignorefile

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



        os_version: Union[None, str]
        os_version = self.os_version

        shm_bytes = self.shm_bytes

        skip_unused_stages = self.skip_unused_stages

        squash: str = self.squash

        unset_environment = self.unset_environment



        unset_labels = self.unset_labels



        timestamp: Union[None, Unset, int]
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
            "ignorefile": ignorefile,
            "jobs": jobs,
            "labels": labels,
            "layer_compression": layer_compression,
            "layer_labels": layer_labels,
            "layers": layers,
            "no_hostname": no_hostname,
            "no_hosts": no_hosts,
            "omit_history": omit_history,
            "os_features": os_features,
            "os_version": os_version,
            "shm_bytes": shm_bytes,
            "skip_unused_stages": skip_unused_stages,
            "squash": squash,
            "unset_environment": unset_environment,
            "unset_labels": unset_labels,
        })
        if timestamp is not UNSET:
            field_dict["timestamp"] = timestamp

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.visual_build_additional_context import VisualBuildAdditionalContext
        from ..models.visual_build_option_value import VisualBuildOptionValue
        d = dict(src_dict)
        additional_contexts = []
        _additional_contexts = d.pop("additional_contexts")
        for additional_contexts_item_data in (_additional_contexts):
            additional_contexts_item = VisualBuildAdditionalContext.from_dict(additional_contexts_item_data)



            additional_contexts.append(additional_contexts_item)


        annotations = []
        _annotations = d.pop("annotations")
        for annotations_item_data in (_annotations):
            annotations_item = VisualBuildOptionValue.from_dict(annotations_item_data)



            annotations.append(annotations_item)


        environment = []
        _environment = d.pop("environment")
        for environment_item_data in (_environment):
            environment_item = VisualBuildOptionValue.from_dict(environment_item_data)



            environment.append(environment_item)


        format_ = check_visual_build_options_format(d.pop("format"))




        identity_label = d.pop("identity_label")

        def _parse_ignorefile(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        ignorefile = _parse_ignorefile(d.pop("ignorefile"))


        jobs = d.pop("jobs")

        labels = []
        _labels = d.pop("labels")
        for labels_item_data in (_labels):
            labels_item = VisualBuildOptionValue.from_dict(labels_item_data)



            labels.append(labels_item)


        layer_compression = check_visual_build_options_layer_compression(d.pop("layer_compression"))




        layer_labels = []
        _layer_labels = d.pop("layer_labels")
        for layer_labels_item_data in (_layer_labels):
            layer_labels_item = VisualBuildOptionValue.from_dict(layer_labels_item_data)



            layer_labels.append(layer_labels_item)


        layers = d.pop("layers")

        no_hostname = d.pop("no_hostname")

        no_hosts = d.pop("no_hosts")

        omit_history = d.pop("omit_history")

        os_features = cast(list[str], d.pop("os_features"))


        def _parse_os_version(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        os_version = _parse_os_version(d.pop("os_version"))


        shm_bytes = d.pop("shm_bytes")

        skip_unused_stages = d.pop("skip_unused_stages")

        squash = check_visual_build_options_squash(d.pop("squash"))




        unset_environment = cast(list[str], d.pop("unset_environment"))


        unset_labels = cast(list[str], d.pop("unset_labels"))


        def _parse_timestamp(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        timestamp = _parse_timestamp(d.pop("timestamp", UNSET))


        visual_build_options = cls(
            additional_contexts=additional_contexts,
            annotations=annotations,
            environment=environment,
            format_=format_,
            identity_label=identity_label,
            ignorefile=ignorefile,
            jobs=jobs,
            labels=labels,
            layer_compression=layer_compression,
            layer_labels=layer_labels,
            layers=layers,
            no_hostname=no_hostname,
            no_hosts=no_hosts,
            omit_history=omit_history,
            os_features=os_features,
            os_version=os_version,
            shm_bytes=shm_bytes,
            skip_unused_stages=skip_unused_stages,
            squash=squash,
            unset_environment=unset_environment,
            unset_labels=unset_labels,
            timestamp=timestamp,
        )

        return visual_build_options
