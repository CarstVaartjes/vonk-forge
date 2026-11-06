"""The LiteLLM ``model_info`` of one route, built only from its recipe's API facts.

The facts are the signed catalog's ``RecipeApi`` document (stored once beside
the recipe revision at ingress; it is not checked again here). The option
choices and setting overrides the run's mapping carries decide which block
applies and what ``{setting: context_tokens}`` means. A route whose recipe
revision has no readable facts publishes no ``model_info`` and stays up.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from sqlalchemy.orm import Session
from vonk_forge_contracts import (
    ChatApi,
    DecisionApi,
    RecipeApi,
    RecipeDefinition,
    read_recipe,
)
from vonk_forge_contracts.api import (
    ApiReasoningAlwaysOn,
    ApiReasoningNone,
    ApiReasoningSwitch,
    ApiRemainingContext,
    ApiTokenArgument,
    ApiTokenSetting,
    ApiToolsSupported,
)
from vonk_forge_contracts.recipe import (
    RecipeDecisionSettings,
    RecipeGenerationSettings,
    Scalar,
)

from .catalog_revision_contract import read_recipe_projection
from .litellm import LiteLlmModelInfo, VonkThinking
from .models import (
    CatalogDocumentRevision,
    ClusterMapping,
    RecipeInstallation,
    RecipeRun,
)
from .recipe_runtime_specs import split_option_choices

_LOGGER = logging.getLogger(__name__)

# Sampling and length controls every OpenAI-compatible chat route reads; the
# rest of ``supported_openai_params`` follows from the typed facts.
_CHAT_PARAMETERS = (
    "max_completion_tokens",
    "max_tokens",
    "temperature",
    "top_p",
)
_DECISION_ENDPOINT = "/v1/systemone"


def route_model_info(
    session: Session, run: RecipeRun, mapping: ClusterMapping | None
) -> LiteLlmModelInfo | None:
    """The run's ``model_info``, or None when its facts are not published.

    Fail open: whatever is missing or unreadable here (no installation, a
    revision stored without facts, an option the recipe no longer has) only
    means the route publishes no ``model_info``. It never blocks or drops a
    route.
    """

    try:
        installation = session.get(RecipeInstallation, run.installation_id)
        revision = (
            session.get(CatalogDocumentRevision, installation.recipe_revision_id)
            if installation is not None
            else None
        )
        if revision is None:
            return None
        api = read_recipe_projection(revision).api
        if api is None:
            return None
        choices, settings = split_option_choices(mapping.parameters if mapping else {})
        return build_model_info(read_recipe(revision.document), api, choices, settings)
    except (TypeError, ValueError, KeyError) as error:
        _LOGGER.warning(
            "route model_info unavailable; the route publishes none: run=%s error=%s",
            run.id,
            type(error).__name__,
        )
    return None


def build_model_info(
    recipe: RecipeDefinition,
    api: RecipeApi,
    choices: dict[str, str],
    settings: dict[str, Scalar],
) -> LiteLlmModelInfo:
    """Apply the mapping's option choices and setting values to the facts."""

    recipe_settings = recipe.settings
    if not isinstance(
        recipe_settings, RecipeGenerationSettings | RecipeDecisionSettings
    ):
        raise TypeError("the recipe does not serve a generation or decision route")
    block = api.for_choices(recipe, choices)
    override = settings.get("context_tokens")
    context = (
        override
        if isinstance(override, int) and not isinstance(override, bool)
        else recipe_settings.context_tokens.value
    )
    modalities = block.input_modalities
    max_input = (
        context
        if block.max_input_tokens is None
        else min(block.max_input_tokens, context)
    )
    if isinstance(block, DecisionApi):
        return LiteLlmModelInfo(
            mode="decision",
            max_input_tokens=max_input,
            supports_vision=modalities.image,
            supports_audio_input=modalities.audio,
            supports_audio_output=False,
            supports_pdf_input=modalities.pdf,
            supports_reasoning=False,
            thinking_always_on=False,
            supports_function_calling=False,
            supports_tool_choice=False,
            supports_parallel_function_calling=False,
            supports_response_schema=False,
            supports_system_messages=False,
            supports_native_streaming=False,
            supported_endpoints=[_DECISION_ENDPOINT],
            supported_openai_params=[],
        )
    assert isinstance(block, ChatApi)
    output = (
        context
        if isinstance(block.max_output_tokens, ApiTokenSetting)
        else min(block.max_output_tokens.value, context)
    )
    reasoning = block.reasoning
    effort = None if isinstance(reasoning, ApiReasoningNone) else reasoning.effort
    tools = block.tools
    tool_choice = tools.tool_choice if isinstance(tools, ApiToolsSupported) else []
    parallel = isinstance(tools, ApiToolsSupported) and tools.parallel_calls
    parameters_supported = {
        *_CHAT_PARAMETERS,
        *(["stream"] if block.streaming else []),
        *(["tools"] if isinstance(tools, ApiToolsSupported) else []),
        *(["tool_choice"] if tool_choice else []),
        *(["parallel_tool_calls"] if parallel else []),
        *(["response_format"] if block.structured_output else []),
        *(["reasoning_effort"] if effort is not None else []),
    }
    return LiteLlmModelInfo(
        mode="chat",
        max_tokens=output,
        max_input_tokens=max_input,
        max_output_tokens=output,
        supports_vision=modalities.image,
        supports_audio_input=modalities.audio,
        supports_audio_output=block.audio_output,
        supports_pdf_input=modalities.pdf,
        supports_reasoning=not isinstance(reasoning, ApiReasoningNone),
        thinking_always_on=isinstance(reasoning, ApiReasoningAlwaysOn),
        reasoning_effort_levels=None if effort is None else list(effort.levels),
        default_reasoning_effort=None if effort is None else effort.default,
        supports_function_calling=isinstance(tools, ApiToolsSupported),
        supports_tool_choice=bool(tool_choice),
        supports_parallel_function_calling=parallel,
        supports_response_schema=block.structured_output,
        supports_system_messages=block.system_messages,
        supports_native_streaming=block.streaming,
        supported_endpoints=list(block.endpoints),
        supported_openai_params=sorted(parameters_supported),
        vonk_thinking=(
            VonkThinking(
                via=reasoning.via,
                keys=list(reasoning.keys),
                on=reasoning.on,
                off=reasoning.off,
                default=reasoning.default,
            )
            if isinstance(reasoning, ApiReasoningSwitch)
            else None
        ),
        vonk_default_output_tokens=_default_output_tokens(
            recipe, block, choices, settings
        ),
    )


def _default_output_tokens(
    recipe: RecipeDefinition,
    block: ChatApi,
    choices: Mapping[str, str],
    settings: dict[str, Scalar],
) -> int | None:
    """The server's default output cap; None when the request may use the rest."""

    default = block.default_output_tokens
    if isinstance(default, ApiRemainingContext):
        return None
    if not isinstance(default, ApiTokenArgument):
        return default.value
    for argument in recipe.with_option_choices(choices).runtime.arguments:
        if argument.name != default.argument:
            continue
        if argument.setting is None:
            value = argument.value
        elif argument.setting in settings:
            value = settings[argument.setting]
        else:
            bound = recipe.settings.knobs.get(argument.setting) or getattr(
                recipe.settings, argument.setting, None
            )
            value = bound.value if bound is not None else None
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    raise ValueError("default_output_tokens names an argument without an integer")
