from typing import Literal

WaitVerdict = Literal['DERIVED', 'FIX-ACTION', 'KEEP', 'SELF-HEAL']

WAIT_VERDICT_VALUES: set[WaitVerdict] = { 'DERIVED', 'FIX-ACTION', 'KEEP', 'SELF-HEAL',  }

def check_wait_verdict(value: str) -> WaitVerdict:
    if value in WAIT_VERDICT_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {WAIT_VERDICT_VALUES!r}")
