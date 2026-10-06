from typing import Literal

ModelFileState = Literal['corrupt', 'missing', 'partial', 'verified']

MODEL_FILE_STATE_VALUES: set[ModelFileState] = { 'corrupt', 'missing', 'partial', 'verified',  }

def check_model_file_state(value: str) -> ModelFileState:
    if value in MODEL_FILE_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {MODEL_FILE_STATE_VALUES!r}")
