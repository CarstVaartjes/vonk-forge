from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.telemetry_runtime_readiness import check_telemetry_runtime_readiness
from ..models.telemetry_runtime_readiness import TelemetryRuntimeReadiness
from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="TelemetryRuntime")



@_attrs_define
class TelemetryRuntime:
    """
        Attributes:
            adapter (str):
            adapter_supported (bool):
            backend (str):
            engine_id (str):
            ranks (list[int]):
            readiness (TelemetryRuntimeReadiness):
            run_id (str):
            serving_node_ids (list[str]):
            adapter_reason (None | str | Unset):
            adapter_version (None | str | Unset):
            context_limit_tokens (int | None | Unset):
            endpoint (None | str | Unset):
            error (None | str | Unset):
            model (None | str | Unset):
            model_version (None | str | Unset):
            recipe_revision (None | str | Unset):
            version (None | str | Unset):
     """

    adapter: str
    adapter_supported: bool
    backend: str
    engine_id: str
    ranks: list[int]
    readiness: TelemetryRuntimeReadiness
    run_id: str
    serving_node_ids: list[str]
    adapter_reason: None | str | Unset = UNSET
    adapter_version: None | str | Unset = UNSET
    context_limit_tokens: int | None | Unset = UNSET
    endpoint: None | str | Unset = UNSET
    error: None | str | Unset = UNSET
    model: None | str | Unset = UNSET
    model_version: None | str | Unset = UNSET
    recipe_revision: None | str | Unset = UNSET
    version: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        adapter = self.adapter

        adapter_supported = self.adapter_supported

        backend = self.backend

        engine_id = self.engine_id

        ranks = self.ranks



        readiness: str = self.readiness

        run_id = self.run_id

        serving_node_ids = self.serving_node_ids



        adapter_reason: None | str | Unset
        if isinstance(self.adapter_reason, Unset):
            adapter_reason = UNSET
        else:
            adapter_reason = self.adapter_reason

        adapter_version: None | str | Unset
        if isinstance(self.adapter_version, Unset):
            adapter_version = UNSET
        else:
            adapter_version = self.adapter_version

        context_limit_tokens: int | None | Unset
        if isinstance(self.context_limit_tokens, Unset):
            context_limit_tokens = UNSET
        else:
            context_limit_tokens = self.context_limit_tokens

        endpoint: None | str | Unset
        if isinstance(self.endpoint, Unset):
            endpoint = UNSET
        else:
            endpoint = self.endpoint

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        model: None | str | Unset
        if isinstance(self.model, Unset):
            model = UNSET
        else:
            model = self.model

        model_version: None | str | Unset
        if isinstance(self.model_version, Unset):
            model_version = UNSET
        else:
            model_version = self.model_version

        recipe_revision: None | str | Unset
        if isinstance(self.recipe_revision, Unset):
            recipe_revision = UNSET
        else:
            recipe_revision = self.recipe_revision

        version: None | str | Unset
        if isinstance(self.version, Unset):
            version = UNSET
        else:
            version = self.version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "adapter": adapter,
            "adapter_supported": adapter_supported,
            "backend": backend,
            "engine_id": engine_id,
            "ranks": ranks,
            "readiness": readiness,
            "run_id": run_id,
            "serving_node_ids": serving_node_ids,
        })
        if adapter_reason is not UNSET:
            field_dict["adapter_reason"] = adapter_reason
        if adapter_version is not UNSET:
            field_dict["adapter_version"] = adapter_version
        if context_limit_tokens is not UNSET:
            field_dict["context_limit_tokens"] = context_limit_tokens
        if endpoint is not UNSET:
            field_dict["endpoint"] = endpoint
        if error is not UNSET:
            field_dict["error"] = error
        if model is not UNSET:
            field_dict["model"] = model
        if model_version is not UNSET:
            field_dict["model_version"] = model_version
        if recipe_revision is not UNSET:
            field_dict["recipe_revision"] = recipe_revision
        if version is not UNSET:
            field_dict["version"] = version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        adapter = d.pop("adapter")

        adapter_supported = d.pop("adapter_supported")

        backend = d.pop("backend")

        engine_id = d.pop("engine_id")

        ranks = cast(list[int], d.pop("ranks"))


        readiness = check_telemetry_runtime_readiness(d.pop("readiness"))




        run_id = d.pop("run_id")

        serving_node_ids = cast(list[str], d.pop("serving_node_ids"))


        def _parse_adapter_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        adapter_reason = _parse_adapter_reason(d.pop("adapter_reason", UNSET))


        def _parse_adapter_version(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        adapter_version = _parse_adapter_version(d.pop("adapter_version", UNSET))


        def _parse_context_limit_tokens(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        context_limit_tokens = _parse_context_limit_tokens(d.pop("context_limit_tokens", UNSET))


        def _parse_endpoint(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        endpoint = _parse_endpoint(d.pop("endpoint", UNSET))


        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))


        def _parse_model(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        model = _parse_model(d.pop("model", UNSET))


        def _parse_model_version(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        model_version = _parse_model_version(d.pop("model_version", UNSET))


        def _parse_recipe_revision(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        recipe_revision = _parse_recipe_revision(d.pop("recipe_revision", UNSET))


        def _parse_version(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        version = _parse_version(d.pop("version", UNSET))


        telemetry_runtime = cls(
            adapter=adapter,
            adapter_supported=adapter_supported,
            backend=backend,
            engine_id=engine_id,
            ranks=ranks,
            readiness=readiness,
            run_id=run_id,
            serving_node_ids=serving_node_ids,
            adapter_reason=adapter_reason,
            adapter_version=adapter_version,
            context_limit_tokens=context_limit_tokens,
            endpoint=endpoint,
            error=error,
            model=model,
            model_version=model_version,
            recipe_revision=recipe_revision,
            version=version,
        )

        return telemetry_runtime
