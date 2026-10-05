from typing import Literal

BlockerCategory = Literal['already-retried', 'bookkeeping-debt', 'input-validation', 'security-edge']

BLOCKER_CATEGORY_VALUES: set[BlockerCategory] = { 'already-retried', 'bookkeeping-debt', 'input-validation', 'security-edge',  }

def check_blocker_category(value: str) -> BlockerCategory:
    if value in BLOCKER_CATEGORY_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {BLOCKER_CATEGORY_VALUES!r}")
