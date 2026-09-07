from typing import Literal, cast

RecipeValidationCheckKind = Literal['artifact-job.output', 'audio-job.output', 'image-job.output', 'mesh-job.output', 'openai.chat', 'openai.completion', 'openai.embedding', 'openai.health', 'openai.tools', 'openai.vision', 'video-job.output']

RECIPE_VALIDATION_CHECK_KIND_VALUES: set[RecipeValidationCheckKind] = { 'artifact-job.output', 'audio-job.output', 'image-job.output', 'mesh-job.output', 'openai.chat', 'openai.completion', 'openai.embedding', 'openai.health', 'openai.tools', 'openai.vision', 'video-job.output',  }

def check_recipe_validation_check_kind(value: str) -> RecipeValidationCheckKind:
    if value in RECIPE_VALIDATION_CHECK_KIND_VALUES:
        return cast(RecipeValidationCheckKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_VALIDATION_CHECK_KIND_VALUES!r}")
