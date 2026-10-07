from typing import Literal

BookkeepingReason = Literal['evidence-mismatch', 'evidence-unavailable', 'persisted-state-damaged', 'row-incomplete']

BOOKKEEPING_REASON_VALUES: set[BookkeepingReason] = { 'evidence-mismatch', 'evidence-unavailable', 'persisted-state-damaged', 'row-incomplete',  }

def check_bookkeeping_reason(value: str) -> BookkeepingReason:
    if value in BOOKKEEPING_REASON_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {BOOKKEEPING_REASON_VALUES!r}")
