from typing import Literal

OutcomeKind = Literal['cancelled', 'done', 'failed', 'unknown']

OUTCOME_KIND_VALUES: set[OutcomeKind] = { 'cancelled', 'done', 'failed', 'unknown',  }

def check_outcome_kind(value: str) -> OutcomeKind:
    if value in OUTCOME_KIND_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {OUTCOME_KIND_VALUES!r}")
