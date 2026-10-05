from typing import Literal

LifecycleEffect = Literal['established', 'issued', 'none', 'stopped', 'unknown']

LIFECYCLE_EFFECT_VALUES: set[LifecycleEffect] = { 'established', 'issued', 'none', 'stopped', 'unknown',  }

def check_lifecycle_effect(value: str) -> LifecycleEffect:
    if value in LIFECYCLE_EFFECT_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIFECYCLE_EFFECT_VALUES!r}")
