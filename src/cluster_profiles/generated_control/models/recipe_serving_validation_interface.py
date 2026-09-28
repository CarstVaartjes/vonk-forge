from typing import Literal

RecipeServingValidationInterface = Literal['artifact-job', 'audio-job', 'image-job', 'mesh-job', 'openai', 'video-job']

RECIPE_SERVING_VALIDATION_INTERFACE_VALUES: set[RecipeServingValidationInterface] = { 'artifact-job', 'audio-job', 'image-job', 'mesh-job', 'openai', 'video-job',  }

def check_recipe_serving_validation_interface(value: str) -> RecipeServingValidationInterface:
    if value in RECIPE_SERVING_VALIDATION_INTERFACE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_SERVING_VALIDATION_INTERFACE_VALUES!r}")
