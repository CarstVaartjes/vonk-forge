from typing import Literal

ModelAccessAuthentication = Literal['none', 'token']

MODEL_ACCESS_AUTHENTICATION_VALUES: set[ModelAccessAuthentication] = { 'none', 'token',  }

def check_model_access_authentication(value: str) -> ModelAccessAuthentication:
    if value in MODEL_ACCESS_AUTHENTICATION_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {MODEL_ACCESS_AUTHENTICATION_VALUES!r}")
