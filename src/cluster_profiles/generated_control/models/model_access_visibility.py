from typing import Literal, cast

ModelAccessVisibility = Literal['public', 'restricted']

MODEL_ACCESS_VISIBILITY_VALUES: set[ModelAccessVisibility] = { 'public', 'restricted',  }

def check_model_access_visibility(value: str) -> ModelAccessVisibility:
    if value in MODEL_ACCESS_VISIBILITY_VALUES:
        return cast(ModelAccessVisibility, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {MODEL_ACCESS_VISIBILITY_VALUES!r}")
