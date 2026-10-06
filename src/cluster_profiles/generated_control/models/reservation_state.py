from typing import Literal

ReservationState = Literal['active', 'expired', 'promised', 'released']

RESERVATION_STATE_VALUES: set[ReservationState] = { 'active', 'expired', 'promised', 'released',  }

def check_reservation_state(value: str) -> ReservationState:
    if value in RESERVATION_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RESERVATION_STATE_VALUES!r}")
