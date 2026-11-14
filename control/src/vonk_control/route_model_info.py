"""The LiteLLM ``model_info`` of one route, built only from its recipe's API facts.

The facts are the signed catalog's ``RecipeApi`` document (stored once beside
the recipe revision at ingress; it is not checked again here). The option
choices and setting values the run's mapping carries decide which block applies
and what ``{setting: context_tokens}`` means. A route whose recipe revision has
no usable facts publishes no ``model_info`` and stays up.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_forge_contracts import (
    AudioSpeechApi,
    AudioTranscriptionApi,
    ChatApi,
    DecisionApi,
    RecipeApi,
    RecipeDefinition,
    UnrecognisedWord,
)
from vonk_forge_contracts.api import (
    ApiReasoningAlwaysOn,
    ApiReasoningEffort,
    ApiReasoningEffortLevels,
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
    RecipeOptionError,
    Scalar,
)

from .catalog_revision_contract import (
    CatalogRevisionContractError,
    read_catalog_document,
    read_recipe_projection,
)
from .litellm import (
    LiteLlmModelInfo,
    OpenAiRequestParameter,
    VonkAudioSpeech,
    VonkAudioTranscription,
    VonkReasoningEffortLevels,
    VonkReasoningEffortText,
    VonkThinking,
)
from .models import (
    CatalogDocumentRevision,
    ClusterMapping,
    RecipeInstallation,
    RecipeRun,
)
from .recipe_runtime_specs import (
    RecipeRuntimeSpecError,
    _compiled_arguments,
    split_option_choices,
)

_LOGGER = logging.getLogger(__name__)

# Length and sampling controls every OpenAI-compatible chat route reads; the
# rest of ``supported_openai_params`` follows from the typed facts.
_CHAT_PARAMETERS = (
    OpenAiRequestParameter.MAX_COMPLETION_TOKENS,
    OpenAiRequestParameter.MAX_TOKENS,
    OpenAiRequestParameter.STOP,
    OpenAiRequestParameter.TEMPERATURE,
    OpenAiRequestParameter.TOP_P,
)
_DECISION_ENDPOINT = "/v1/systemone"
# One endpoint per audio mode, as in LiteLLM's own audio entries.
_TRANSCRIPTION_ENDPOINT = "/v1/audio/transcriptions"
_SPEECH_ENDPOINT = "/v1/audio/speech"


class ModelInfoError(ValueError):
    """The facts cannot describe this run's route; the route publishes none."""


class RouteModelInfoReader:
    """The ``model_info`` of every run of one candidate observation.

    The installation and revision rows of all runs come from two queries made
    before the loop, and a (revision, mapping generation) pair is derived once.
    The per-run reads that remain are sequential on purpose: the candidate holds
    one session, and a session is not shared across threads (P21).
    """

    def __init__(
        self,
        session: Session,
        runs: Iterable[RecipeRun],
        degraded: Callable[[str, str | None], None],
    ) -> None:
        installations = {
            row.id: row
            for row in session.scalars(
                select(RecipeInstallation).where(
                    RecipeInstallation.id.in_({run.installation_id for run in runs})
                )
            )
        }
        revisions = {
            row.id: row
            for row in session.scalars(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.id.in_(
                        {row.recipe_revision_id for row in installations.values()}
                    )
                )
            )
        }
        self._revision_of = {
            installation_id: revisions.get(row.recipe_revision_id)
            for installation_id, row in installations.items()
        }
        self._degraded = degraded
        self._derived: dict[
            tuple[str, str, int], LiteLlmModelInfo | ModelInfoError | None
        ] = {}

    def for_run(
        self, run: RecipeRun, mapping: ClusterMapping | None
    ) -> LiteLlmModelInfo | None:
        """The run's ``model_info``; None when its facts are absent or unusable.

        Fail open: facts that are absent are silent; facts that exist but cannot
        be used are reported once per change through ``degraded``. Nothing here
        blocks or drops a route.
        """

        revision = self._revision_of.get(run.installation_id)
        if revision is None:
            return None
        key = (revision.id, run.mapping_id, run.mapping_generation)
        if key not in self._derived:
            self._derived[key] = self._derive(revision, mapping)
        derived = self._derived[key]
        if isinstance(derived, ModelInfoError):
            self._degraded(run.id, str(derived)[:200])
            return None
        self._degraded(run.id, None)
        return derived

    @staticmethod
    def _derive(
        revision: CatalogDocumentRevision, mapping: ClusterMapping | None
    ) -> LiteLlmModelInfo | ModelInfoError | None:
        try:
            api = read_recipe_projection(revision).api
            if api is None:
                return None
            recipe = read_catalog_document(revision)
            if not isinstance(recipe, RecipeDefinition):
                raise ModelInfoError("the revision is not a recipe")
            choices, settings = split_option_choices(
                mapping.parameters if mapping is not None else {}
            )
            return build_model_info(recipe, api, choices, settings)
        except (
            CatalogRevisionContractError,
            RecipeOptionError,
            RecipeRuntimeSpecError,
            ValidationError,
            ModelInfoError,
        ) as error:
            _LOGGER.warning(
                "route model_info unavailable; the route publishes none: "
                "revision=%s error=%s",
                revision.id,
                str(error)[:200],
            )
            return ModelInfoError(f"{type(error).__name__}: {str(error)[:160]}")


def build_model_info(
    recipe: RecipeDefinition,
    api: RecipeApi,
    choices: dict[str, str],
    settings: dict[str, Scalar],
) -> LiteLlmModelInfo:
    """Apply the mapping's option choices and setting values to the facts."""

    block = api.for_choices(recipe, choices)
    # Audio blocks carry no token limits, so they read no context setting and
    # branch before anything below reads ``input_modalities``.
    if isinstance(block, AudioTranscriptionApi):
        return _transcription_model_info(block)
    if isinstance(block, AudioSpeechApi):
        return _speech_model_info(block)
    recipe_settings = recipe.settings
    if not isinstance(
        recipe_settings, RecipeGenerationSettings | RecipeDecisionSettings
    ):
        raise ModelInfoError("the recipe does not serve a generation or decision route")
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
            supports_video_input=modalities.video,
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
    # LiteLLM's effort fields and the standard parameter name are published only
    # for the one request shape they promise: the top-level ``reasoning_effort``.
    # A route that takes its effort elsewhere (inside chat_template_kwargs, or
    # under another key) ignores a top-level one, so no client may be told to send it.
    standard_effort = (
        effort
        if effort is not None
        and effort.via == "top_level"
        and effort.key == OpenAiRequestParameter.REASONING_EFFORT
        else None
    )
    parameters_supported = {
        *_CHAT_PARAMETERS,
        *([OpenAiRequestParameter.STREAM] if block.streaming else []),
        *(
            [OpenAiRequestParameter.TOOLS]
            if isinstance(tools, ApiToolsSupported)
            else []
        ),
        *([OpenAiRequestParameter.TOOL_CHOICE] if tool_choice else []),
        *([OpenAiRequestParameter.PARALLEL_TOOL_CALLS] if parallel else []),
        *([OpenAiRequestParameter.RESPONSE_FORMAT] if block.structured_output else []),
        *([OpenAiRequestParameter.REASONING_EFFORT] if standard_effort else []),
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
        supports_video_input=modalities.video,
        supports_reasoning=not isinstance(reasoning, ApiReasoningNone),
        thinking_always_on=isinstance(reasoning, ApiReasoningAlwaysOn),
        reasoning_effort_levels=(
            list(standard_effort.levels)
            if isinstance(standard_effort, ApiReasoningEffortLevels)
            else None
        ),
        default_reasoning_effort=(
            None if standard_effort is None else standard_effort.default
        ),
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
            # Stored facts carry only known words (the sync withholds the
            # rest); an unrecognised one is never acted on.
            if isinstance(reasoning, ApiReasoningSwitch)
            and not isinstance(reasoning.via, UnrecognisedWord)
            and not isinstance(reasoning.default, UnrecognisedWord)
            else None
        ),
        vonk_reasoning_effort=_vonk_reasoning_effort(effort),
        vonk_default_output_tokens=_default_output_tokens(
            recipe, block, choices, settings
        ),
    )


def _vonk_reasoning_effort(
    effort: ApiReasoningEffort | None,
) -> VonkReasoningEffortLevels | VonkReasoningEffortText | None:
    """The recipe's effort block as stated, whichever kind it is."""
    if effort is None or isinstance(effort.via, UnrecognisedWord):
        return None
    if isinstance(effort, ApiReasoningEffortLevels):
        return VonkReasoningEffortLevels(
            kind="levels",
            via=effort.via,
            key=effort.key,
            levels=list(effort.levels),
            default=effort.default,
        )
    if isinstance(effort.default, UnrecognisedWord):
        return None
    return VonkReasoningEffortText(
        kind="text", via=effort.via, key=effort.key, default=effort.default
    )


def _audio_model_info(**fields: object) -> LiteLlmModelInfo:
    """The fields every audio mode states the same way: not applicable is false."""

    return LiteLlmModelInfo.model_validate(
        {
            "supports_vision": False,
            "supports_pdf_input": False,
            "supports_video_input": False,
            "supports_reasoning": False,
            "thinking_always_on": False,
            "supports_function_calling": False,
            "supports_tool_choice": False,
            "supports_parallel_function_calling": False,
            "supports_response_schema": False,
            "supports_system_messages": False,
            "supports_native_streaming": False,
            **fields,
        }
    )


def _transcription_model_info(block: AudioTranscriptionApi) -> LiteLlmModelInfo:
    parameters = {
        OpenAiRequestParameter.LANGUAGE,
        OpenAiRequestParameter.RESPONSE_FORMAT,
        *([OpenAiRequestParameter.TEMPERATURE] if block.temperature else []),
        *([OpenAiRequestParameter.PROMPT] if block.prompt else []),
        *(
            [OpenAiRequestParameter.TIMESTAMP_GRANULARITIES]
            if block.timestamp_granularities
            else []
        ),
    }
    return _audio_model_info(
        mode="audio_transcription",
        supports_audio_input=True,
        supports_audio_output=False,
        supported_endpoints=[_TRANSCRIPTION_ENDPOINT],
        supported_openai_params=sorted(parameters),
        vonk_audio_transcription=VonkAudioTranscription(
            **block.model_dump(exclude={"mode"})
        ),
    )


def _speech_model_info(block: AudioSpeechApi) -> LiteLlmModelInfo:
    parameters = {
        OpenAiRequestParameter.VOICE,
        OpenAiRequestParameter.RESPONSE_FORMAT,
        *([OpenAiRequestParameter.SPEED] if block.speed else []),
        *([OpenAiRequestParameter.INSTRUCTIONS] if block.instructions else []),
    }
    return _audio_model_info(
        mode="audio_speech",
        supports_audio_input=False,
        supports_audio_output=True,
        supported_endpoints=[_SPEECH_ENDPOINT],
        supported_openai_params=sorted(parameters),
        supported_modalities=["text"],
        supported_output_modalities=["audio"],
        health_check_voice=block.voices[0],
        vonk_audio_speech=VonkAudioSpeech(**block.model_dump(exclude={"mode"})),
    )


def _default_output_tokens(
    recipe: RecipeDefinition,
    block: ChatApi,
    choices: dict[str, str],
    settings: dict[str, Scalar],
) -> int | None:
    """The server's default output cap; None when the request may use the rest."""

    default = block.default_output_tokens
    if isinstance(default, ApiRemainingContext):
        return None
    if not isinstance(default, ApiTokenArgument):
        return default.value
    try:
        arguments = _compiled_arguments(recipe.with_option_choices(choices), settings)
    except KeyError as error:
        raise ModelInfoError("an argument names a setting the recipe lacks") from error
    for argument in arguments:
        if argument.name == default.argument:
            if isinstance(argument.value, int) and not isinstance(argument.value, bool):
                return argument.value
            break
    raise ModelInfoError("default_output_tokens names an argument without an integer")
