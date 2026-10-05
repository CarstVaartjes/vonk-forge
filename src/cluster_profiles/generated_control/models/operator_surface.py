from typing import Literal

OperatorSurface = Literal['automatic', 'resume', 'retire', 'retry', 'stop']

OPERATOR_SURFACE_VALUES: set[OperatorSurface] = { 'automatic', 'resume', 'retire', 'retry', 'stop',  }

def check_operator_surface(value: str) -> OperatorSurface:
    if value in OPERATOR_SURFACE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {OPERATOR_SURFACE_VALUES!r}")
