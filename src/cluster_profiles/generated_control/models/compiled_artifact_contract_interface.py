from typing import Literal, cast

CompiledArtifactContractInterface = Literal['artifact-job', 'audio-job', 'image-job', 'mesh-job', 'video-job']

COMPILED_ARTIFACT_CONTRACT_INTERFACE_VALUES: set[CompiledArtifactContractInterface] = { 'artifact-job', 'audio-job', 'image-job', 'mesh-job', 'video-job',  }

def check_compiled_artifact_contract_interface(value: str) -> CompiledArtifactContractInterface:
    if value in COMPILED_ARTIFACT_CONTRACT_INTERFACE_VALUES:
        return cast(CompiledArtifactContractInterface, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {COMPILED_ARTIFACT_CONTRACT_INTERFACE_VALUES!r}")
