from typing import Literal, cast

RunSwitchRuntimeImageReferenceIntentSource = Literal['controller-build', 'published']

RUN_SWITCH_RUNTIME_IMAGE_REFERENCE_INTENT_SOURCE_VALUES: set[RunSwitchRuntimeImageReferenceIntentSource] = { 'controller-build', 'published',  }

def check_run_switch_runtime_image_reference_intent_source(value: str) -> RunSwitchRuntimeImageReferenceIntentSource:
    if value in RUN_SWITCH_RUNTIME_IMAGE_REFERENCE_INTENT_SOURCE_VALUES:
        return cast(RunSwitchRuntimeImageReferenceIntentSource, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_RUNTIME_IMAGE_REFERENCE_INTENT_SOURCE_VALUES!r}")
