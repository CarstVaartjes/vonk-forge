from typing import Literal, cast

ModelCapabilityFactSupport = Literal['supported', 'unknown', 'unsupported']

MODEL_CAPABILITY_FACT_SUPPORT_VALUES: set[ModelCapabilityFactSupport] = { 'supported', 'unknown', 'unsupported',  }

def check_model_capability_fact_support(value: str) -> ModelCapabilityFactSupport:
    if value in MODEL_CAPABILITY_FACT_SUPPORT_VALUES:
        return cast(ModelCapabilityFactSupport, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {MODEL_CAPABILITY_FACT_SUPPORT_VALUES!r}")
