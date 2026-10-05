from typing import Literal

StateAlias = Literal['cancelling', 'expired', 'partial', 'waiting', 'waiting-for-operator']

STATE_ALIAS_VALUES: set[StateAlias] = { 'cancelling', 'expired', 'partial', 'waiting', 'waiting-for-operator',  }

def check_state_alias(value: str) -> StateAlias:
    if value in STATE_ALIAS_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {STATE_ALIAS_VALUES!r}")
