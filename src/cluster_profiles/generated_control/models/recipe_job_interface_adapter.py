from typing import Literal, cast

RecipeJobInterfaceAdapter = Literal['artifact-job', 'audio-job', 'image-job', 'mesh-job', 'video-job']

RECIPE_JOB_INTERFACE_ADAPTER_VALUES: set[RecipeJobInterfaceAdapter] = { 'artifact-job', 'audio-job', 'image-job', 'mesh-job', 'video-job',  }

def check_recipe_job_interface_adapter(value: str) -> RecipeJobInterfaceAdapter:
    if value in RECIPE_JOB_INTERFACE_ADAPTER_VALUES:
        return cast(RecipeJobInterfaceAdapter, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_JOB_INTERFACE_ADAPTER_VALUES!r}")
