from typing import Literal

SupersedeCode = Literal['effects-changed-during-admission', 'superseded-by-intent', 'superseded-by-retry']

SUPERSEDE_CODE_VALUES: set[SupersedeCode] = { 'effects-changed-during-admission', 'superseded-by-intent', 'superseded-by-retry',  }

def check_supersede_code(value: str) -> SupersedeCode:
    if value in SUPERSEDE_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {SUPERSEDE_CODE_VALUES!r}")
