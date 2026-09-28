from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.run_switch_runtime_image_reference_intent_source import check_run_switch_runtime_image_reference_intent_source
from ..models.run_switch_runtime_image_reference_intent_source import RunSwitchRuntimeImageReferenceIntentSource
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="RunSwitchRuntimeImageReferenceIntent")



@_attrs_define
class RunSwitchRuntimeImageReferenceIntent:
    """ Exact image bytes provisionally protected by a current RunSwitch job.

        Attributes:
            actor (str):
            archive_sha256 (str):
            execution_keys (list[str]):
            image_bytes (int):
            image_digest (str):
            item_index (int):
            operation_id (str):
            owner_kind (Literal['run-switch-job']):
            phase_index (int):
            plan_digest (str):
            recipe_revision_id (str):
            request_key (str):
            source (RunSwitchRuntimeImageReferenceIntentSource):
            workload_intent_ordinal (int):
            build_id (None | str | Unset):
            build_input_sha256 (None | str | Unset):
            profile_application_id (None | str | Unset):
            registry_manifest_digest (None | str | Unset):
            schema_version (Literal[2] | Unset):  Default: 2.
     """

    actor: str
    archive_sha256: str
    execution_keys: list[str]
    image_bytes: int
    image_digest: str
    item_index: int
    operation_id: str
    owner_kind: Literal['run-switch-job']
    phase_index: int
    plan_digest: str
    recipe_revision_id: str
    request_key: str
    source: RunSwitchRuntimeImageReferenceIntentSource
    workload_intent_ordinal: int
    build_id: None | str | Unset = UNSET
    build_input_sha256: None | str | Unset = UNSET
    profile_application_id: None | str | Unset = UNSET
    registry_manifest_digest: None | str | Unset = UNSET
    schema_version: Literal[2] | Unset = 2





    def to_dict(self) -> dict[str, Any]:
        actor = self.actor

        archive_sha256 = self.archive_sha256

        execution_keys = self.execution_keys



        image_bytes = self.image_bytes

        image_digest = self.image_digest

        item_index = self.item_index

        operation_id = self.operation_id

        owner_kind = self.owner_kind

        phase_index = self.phase_index

        plan_digest = self.plan_digest

        recipe_revision_id = self.recipe_revision_id

        request_key = self.request_key

        source: str = self.source

        workload_intent_ordinal = self.workload_intent_ordinal

        build_id: None | str | Unset
        if isinstance(self.build_id, Unset):
            build_id = UNSET
        else:
            build_id = self.build_id

        build_input_sha256: None | str | Unset
        if isinstance(self.build_input_sha256, Unset):
            build_input_sha256 = UNSET
        else:
            build_input_sha256 = self.build_input_sha256

        profile_application_id: None | str | Unset
        if isinstance(self.profile_application_id, Unset):
            profile_application_id = UNSET
        else:
            profile_application_id = self.profile_application_id

        registry_manifest_digest: None | str | Unset
        if isinstance(self.registry_manifest_digest, Unset):
            registry_manifest_digest = UNSET
        else:
            registry_manifest_digest = self.registry_manifest_digest

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "actor": actor,
            "archive_sha256": archive_sha256,
            "execution_keys": execution_keys,
            "image_bytes": image_bytes,
            "image_digest": image_digest,
            "item_index": item_index,
            "operation_id": operation_id,
            "owner_kind": owner_kind,
            "phase_index": phase_index,
            "plan_digest": plan_digest,
            "recipe_revision_id": recipe_revision_id,
            "request_key": request_key,
            "source": source,
            "workload_intent_ordinal": workload_intent_ordinal,
        })
        if build_id is not UNSET:
            field_dict["build_id"] = build_id
        if build_input_sha256 is not UNSET:
            field_dict["build_input_sha256"] = build_input_sha256
        if profile_application_id is not UNSET:
            field_dict["profile_application_id"] = profile_application_id
        if registry_manifest_digest is not UNSET:
            field_dict["registry_manifest_digest"] = registry_manifest_digest
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        actor = d.pop("actor")

        archive_sha256 = d.pop("archive_sha256")

        execution_keys = cast(list[str], d.pop("execution_keys"))


        image_bytes = d.pop("image_bytes")

        image_digest = d.pop("image_digest")

        item_index = d.pop("item_index")

        operation_id = d.pop("operation_id")

        owner_kind = cast(Literal['run-switch-job'] , d.pop("owner_kind"))
        if owner_kind != 'run-switch-job':
            raise ValueError(f"owner_kind must match const 'run-switch-job', got '{owner_kind}'")

        phase_index = d.pop("phase_index")

        plan_digest = d.pop("plan_digest")

        recipe_revision_id = d.pop("recipe_revision_id")

        request_key = d.pop("request_key")

        source = check_run_switch_runtime_image_reference_intent_source(d.pop("source"))




        workload_intent_ordinal = d.pop("workload_intent_ordinal")

        def _parse_build_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        build_id = _parse_build_id(d.pop("build_id", UNSET))


        def _parse_build_input_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        build_input_sha256 = _parse_build_input_sha256(d.pop("build_input_sha256", UNSET))


        def _parse_profile_application_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        profile_application_id = _parse_profile_application_id(d.pop("profile_application_id", UNSET))


        def _parse_registry_manifest_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        registry_manifest_digest = _parse_registry_manifest_digest(d.pop("registry_manifest_digest", UNSET))


        schema_version = cast(Literal[2] | Unset , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        run_switch_runtime_image_reference_intent = cls(
            actor=actor,
            archive_sha256=archive_sha256,
            execution_keys=execution_keys,
            image_bytes=image_bytes,
            image_digest=image_digest,
            item_index=item_index,
            operation_id=operation_id,
            owner_kind=owner_kind,
            phase_index=phase_index,
            plan_digest=plan_digest,
            recipe_revision_id=recipe_revision_id,
            request_key=request_key,
            source=source,
            workload_intent_ordinal=workload_intent_ordinal,
            build_id=build_id,
            build_input_sha256=build_input_sha256,
            profile_application_id=profile_application_id,
            registry_manifest_digest=registry_manifest_digest,
            schema_version=schema_version,
        )

        return run_switch_runtime_image_reference_intent
