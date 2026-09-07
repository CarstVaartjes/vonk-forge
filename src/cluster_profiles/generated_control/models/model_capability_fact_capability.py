from typing import Literal, cast

ModelCapabilityFactCapability = Literal['3d-generation', 'audio-generation', 'audio-understanding', 'chat', 'code-generation', 'embeddings', 'image-editing', 'image-generation', 'image-understanding', 'ocr', 'reasoning', 'text-generation', 'text-understanding', 'tool-use', 'video-generation', 'video-understanding']

MODEL_CAPABILITY_FACT_CAPABILITY_VALUES: set[ModelCapabilityFactCapability] = { '3d-generation', 'audio-generation', 'audio-understanding', 'chat', 'code-generation', 'embeddings', 'image-editing', 'image-generation', 'image-understanding', 'ocr', 'reasoning', 'text-generation', 'text-understanding', 'tool-use', 'video-generation', 'video-understanding',  }

def check_model_capability_fact_capability(value: str) -> ModelCapabilityFactCapability:
    if value in MODEL_CAPABILITY_FACT_CAPABILITY_VALUES:
        return cast(ModelCapabilityFactCapability, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {MODEL_CAPABILITY_FACT_CAPABILITY_VALUES!r}")
