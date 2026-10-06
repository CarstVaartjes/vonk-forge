"""Compile canonical public RecipeDefinition documents into agent runtimes.

RecipeDefinition and ModelDefinition are the only authoring authorities.  The
compiler consumes their validated projections plus a package/build handle and
adds the platform execution invariants at the final boundary.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol.recipe_jobs import MAX_TIMEOUT_SECONDS
from vonk_forge_contracts import (
    ModelDefinition,
    RecipeDefinition,
    RecipeOptionError,
    document_sha256,
    read_recipe,
)
from vonk_forge_contracts.recipe import RecipeTopology, Scalar
from vonk_forge_contracts.resolver import (
    ContractResolutionError,
    validate_recipe_package_paths,
)

from .catalog_revision_contract import read_model_set
from .harnesses.canonical import compile_canonical_harness
from .harnesses.common import HarnessCompileError
from .models import CatalogDocumentRevision
from .platform_ports import serving_port
from .runtime_spec_contract import (
    RuntimeSpec,
    SpecArgument,
    SpecEndpoint,
    SpecEnvironmentEntry,
    SpecIdentity,
    SpecJob,
    SpecLifecycle,
    SpecModelDependency,
    SpecModelMount,
    SpecPlacementEnvironment,
    SpecRuntime,
    SpecSecurity,
    SpecTelemetry,
    SpecTopology,
    SpecWritablePath,
)
from .runtime_writable_paths import document as writable_path_document

RUNTIME_INTERFACE = "vonk.runtime.v1"

# The effective recipe-option choices travel beside the setting values in the
# mapping parameters. Setting values are scalars, so an object value cannot be
# mistaken for a setting.
OPTION_CHOICES_KEY = "option_choices"


class RecipeRuntimeSpecError(ValueError):
    """The canonical recipe cannot produce a secure runtime projection."""


_SETTING_VALUES = TypeAdapter(dict[str, Scalar])


def split_option_choices(
    parameters: Mapping[str, object] | None,
) -> tuple[dict[str, str], dict[str, Scalar]]:
    """Separate the recipe-option choices from the setting values."""

    settings = dict(parameters or {})
    raw = settings.pop(OPTION_CHOICES_KEY, None)
    try:
        values = _SETTING_VALUES.validate_python(settings, strict=True)
    except ValidationError as error:
        raise RecipeRuntimeSpecError("recipe setting values are invalid") from error
    if raw is None:
        return {}, values
    if not isinstance(raw, Mapping) or any(
        type(name) is not str or type(value) is not str for name, value in raw.items()
    ):
        raise RecipeRuntimeSpecError("recipe option choices are invalid")
    return dict(raw), values


def recipe_topology(value: object) -> RecipeTopology:
    """Return the validated topology; mode, world size, fabric and stop
    order are derived properties of it."""
    return _recipe(value).topology


def _recipe(value: object) -> RecipeDefinition:
    if isinstance(value, RecipeDefinition):
        return value
    raw = getattr(value, "document", value)
    if not isinstance(raw, Mapping):
        raise RecipeRuntimeSpecError("recipe projection is invalid")
    try:
        return read_recipe(raw)
    except Exception as error:
        raise RecipeRuntimeSpecError(
            "recipe does not satisfy RecipeDefinition v2"
        ) from error


def _models(value: object) -> dict[str, ModelDefinition]:
    try:
        return read_model_set(value)
    except Exception as error:
        raise RecipeRuntimeSpecError("canonical model projection is invalid") from error


def _artifact_inputs(package: object) -> dict[str, str]:
    raw = (
        package.get("artifact_inputs")
        if isinstance(package, Mapping)
        else getattr(package, "artifact_inputs", None)
    )
    if raw is None:
        return {}
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise RecipeRuntimeSpecError("package artifact projection is invalid")
    result: dict[str, str] = {}
    for item in raw:
        if (
            not isinstance(item, Mapping)
            or type(item.get("selection_id")) is not str
            or type(item.get("artifact_key")) is not str
        ):
            raise RecipeRuntimeSpecError("package artifact projection is invalid")
        if item["selection_id"] in result:
            raise RecipeRuntimeSpecError(
                "package artifact projection repeats a selection"
            )
        result[item["selection_id"]] = item["artifact_key"]
    return result


def _package_paths(package: object) -> Sequence[str] | None:
    raw = (
        package.get("paths")
        if isinstance(package, Mapping)
        else getattr(package, "paths", None)
    )
    if isinstance(package, Mapping) and "member_paths" in package:
        raise RecipeRuntimeSpecError("package contains retired member_paths authority")
    if raw is None:
        return None
    if (
        not isinstance(raw, Sequence)
        or isinstance(raw, (str, bytes))
        or any(type(path) is not str for path in raw)
    ):
        raise RecipeRuntimeSpecError("package paths are invalid")
    return raw


def compile_runtime_spec(
    recipe: RecipeDefinition,
    *,
    recipe_digest: str,
    models: Mapping[str, ModelDefinition],
    package_handle: object,
    parameters: Mapping[str, Scalar] | None = None,
    option_choices: Mapping[str, str] | None = None,
    role: str,
    rank: int,
) -> RuntimeSpec:
    """Compile one canonical recipe role into a secure runtime transport.

    Only canonical model projections and a package/build handle are accepted;
    retired harness/distribution/patch identities have no place to enter.
    """
    parsed = recipe
    choices = dict(option_choices or {})
    settings = dict(parameters or {})
    if parsed.options or choices:
        # The chosen values become ordinary runtime arguments and environment,
        # so the platform checks below apply to them exactly as to authored ones.
        try:
            parsed = parsed.with_option_choices(choices)
        except RecipeOptionError as error:
            raise RecipeRuntimeSpecError(str(error)) from error
    if type(role) is not str or not role:
        raise RecipeRuntimeSpecError("mapped role is invalid")
    if type(rank) is not int or isinstance(rank, bool) or rank < 0:
        raise RecipeRuntimeSpecError("mapped rank is invalid")
    package = package_handle
    try:
        # The package handle is also the exact closure authority when member
        # paths are available.  Source/build inputs are never inferred from a
        # recipe document copy.
        paths = _package_paths(package)
        if paths is not None:
            validate_recipe_package_paths(parsed, paths)
        projection, artifacts, _image_digest = compile_canonical_harness(
            parsed,
            models,
            package,
            role=role,
            rank=rank,
            settings=settings,
        )
    except (
        HarnessCompileError,
        ContractResolutionError,
        ValueError,
        TypeError,
    ) as error:
        raise RecipeRuntimeSpecError(str(error)) from error
    binding = projection.binding
    if binding is None:
        raise RecipeRuntimeSpecError("canonical harness binding is missing")
    interface = parsed.interfaces[0]
    telemetry = projection.telemetry
    if telemetry is None:
        # The canonical harness compiler always attaches the engine telemetry
        # contract; a projection without one is an internal contract failure,
        # not a runtime default.
        raise RecipeRuntimeSpecError("canonical harness telemetry is missing")
    writable = writable_path_document(
        parsed.runtime.engine, projection.environment, projection.writable_paths
    )
    runtime = SpecRuntime(
        interface=RUNTIME_INTERFACE,
        adapter=projection.slug,
        adapter_version=projection.contract_version,
        telemetry=SpecTelemetry(
            engine=parsed.runtime.engine,
            engine_version=None,
            metrics_format=(
                None
                if telemetry.path is None
                else "comfyui-queue"
                if telemetry.adapter == "comfyui"
                else "prometheus"
            ),
            metrics_path=telemetry.path,
        ),
        image=projection.image,
        architecture="linux/arm64",
        entrypoint=list(projection.command),
        arguments=_compiled_arguments(parsed, settings),
        environment=[
            SpecEnvironmentEntry(name=name, value=value)
            for name, value in projection.environment
        ],
        writable_paths=[SpecWritablePath.model_validate(item) for item in writable],
        placement_environment=(
            SpecPlacementEnvironment() if parsed.topology.node_count > 1 else None
        ),
    )
    artifact_inputs = _artifact_inputs(package)
    dependencies = [
        SpecModelDependency(
            selection_id=selection.id,
            publisher=selection.model.publisher,
            slug=selection.model.slug,
            content_sha256=selection.model.content_sha256,
            artifact_key=artifact_inputs.get(selection.id),
        )
        for selection in parsed.models
    ]
    # Model and input mounts are read-only; the output mount is writable.
    mounts = [
        SpecModelMount(source=mount.source, target=mount.target)
        for mount in projection.model_mounts
    ]
    mounts.append(SpecModelMount(source="/run/vonk/outputs", target="/outputs"))
    if projection.input_mount is not None:
        mounts.append(SpecModelMount(source="/run/vonk/inputs", target="/inputs"))
    spec = RuntimeSpec(
        identity=SpecIdentity(
            recipe_revision_sha256=recipe_digest,
            model_dependencies=dependencies,
            harness_sha256=binding.harness_content_sha256,
            execution_sha256=None,
            build_input_sha256=_build_input_digest(package),
        ),
        model_dependencies=dependencies,
        runtime=runtime,
        # These are compiler output, assembled from ModelDefinition selectors;
        # they are not a second recipe authoring authority.
        artifacts=list(artifacts),
        security=SpecSecurity(
            gpu=projection.gpu,
            user=projection.user,
            network_mode=projection.network_mode,
            mounts=mounts,
        ),
        lifecycle=SpecLifecycle(
            stop_timeout_seconds=parsed.runtime.lifecycle.stop_timeout_seconds
        ),
        topology=SpecTopology(
            name=parsed.topology.name,
            node_count=parsed.topology.node_count,
            rank=rank,
            role=role,
        ),
        endpoint=(
            SpecEndpoint(
                port=serving_port(
                    interface.port, node_count=parsed.topology.node_count
                ),
                model_aliases=list(interface.model_aliases),
                health_path=interface.health_path,
            )
            if interface.adapter == "openai"
            else None
        ),
        job=(
            None
            if interface.adapter == "openai"
            else SpecJob(
                interface=interface.adapter,
                input=interface.input,
                timeout_seconds=MAX_TIMEOUT_SECONDS,
            )
        ),
    )
    spec.identity.execution_sha256 = spec.compile_identity_sha256()
    return spec


def _build_input_digest(package: object) -> str | None:
    raw = (
        package.get("build_input_sha256")
        if isinstance(package, Mapping)
        else getattr(package, "build_input_sha256", None)
    )
    if raw is None:
        raw = (
            package.get("build_input_digest")
            if isinstance(package, Mapping)
            else getattr(package, "build_input_digest", None)
        )
    if raw is None:
        return None
    if type(raw) is not str:
        raise RecipeRuntimeSpecError("build input digest is invalid")
    value = raw.removeprefix("sha256:")
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise RecipeRuntimeSpecError("build input digest is invalid")
    return value


def _compiled_arguments(
    recipe: RecipeDefinition, supplied: Mapping[str, Scalar] | None
) -> list[SpecArgument]:
    values: dict[str, Scalar] = {}
    for name in ("context_tokens", "concurrency", "max_batch_tokens"):
        setting = getattr(recipe.settings, name, None)
        if setting is not None:
            values[name] = setting.value
    values.update(
        {name: setting.value for name, setting in recipe.settings.knobs.items()}
    )
    if supplied:
        values.update(supplied)
    return [
        SpecArgument(
            name=argument.name,
            value=argument.value
            if argument.setting is None
            else values[argument.setting],
        )
        for argument in recipe.runtime.arguments
    ]


@dataclass(frozen=True, slots=True)
class ResolvedRecipe:
    """A canonical recipe with the exact active Model revisions it selects."""

    recipe: RecipeDefinition
    recipe_digest: str
    model_revisions: tuple[CatalogDocumentRevision, ...]

    @property
    def models(self) -> dict[str, ModelDefinition]:
        """The Model documents keyed by the digest recipes reference."""

        return read_model_set(self.model_revisions)


def resolve_recipe_entities(
    session: Session, document: Mapping[str, object]
) -> ResolvedRecipe:
    """Resolve a canonical recipe and its exact active Model revisions."""
    try:
        recipe = read_recipe(document)
    except (TypeError, ValueError) as error:
        raise RecipeRuntimeSpecError(
            "recipe does not satisfy the canonical contract"
        ) from error

    models: list[CatalogDocumentRevision] = []
    for selection in recipe.models:
        reference = selection.model
        revision = session.scalar(
            select(CatalogDocumentRevision)
            .where(
                CatalogDocumentRevision.kind == "model",
                CatalogDocumentRevision.publisher == reference.publisher,
                CatalogDocumentRevision.slug == reference.slug,
                CatalogDocumentRevision.content_digest == reference.content_sha256,
                CatalogDocumentRevision.state == "active",
            )
            .limit(1)
        )
        if revision is None:
            raise RecipeRuntimeSpecError("exact recipe model is not active")
        models.append(revision)
    return ResolvedRecipe(
        recipe=recipe,
        recipe_digest=document_sha256(document),
        model_revisions=tuple(models),
    )


__all__ = [
    "RecipeRuntimeSpecError",
    "ResolvedRecipe",
    "compile_runtime_spec",
    "resolve_recipe_entities",
]
