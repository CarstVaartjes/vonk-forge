from typing import Literal

StopOutcome = Literal['confirmed', 'unconfirmed']

STOP_OUTCOME_VALUES: set[StopOutcome] = { 'confirmed', 'unconfirmed',  }

def check_stop_outcome(value: str) -> StopOutcome:
    if value in STOP_OUTCOME_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {STOP_OUTCOME_VALUES!r}")
