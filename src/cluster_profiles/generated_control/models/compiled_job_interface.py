from typing import Literal, cast

CompiledJobInterface = Literal['artifact-job', 'audio-job', 'image-job', 'mesh-job', 'video-job']

COMPILED_JOB_INTERFACE_VALUES: set[CompiledJobInterface] = { 'artifact-job', 'audio-job', 'image-job', 'mesh-job', 'video-job',  }

def check_compiled_job_interface(value: str) -> CompiledJobInterface:
    if value in COMPILED_JOB_INTERFACE_VALUES:
        return cast(CompiledJobInterface, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {COMPILED_JOB_INTERFACE_VALUES!r}")
