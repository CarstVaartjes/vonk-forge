from typing import Literal

StateWriteKind = Literal['attribute', 'bulk-update', 'constructor', 'dict-item', 'helper-call']

STATE_WRITE_KIND_VALUES: set[StateWriteKind] = { 'attribute', 'bulk-update', 'constructor', 'dict-item', 'helper-call',  }

def check_state_write_kind(value: str) -> StateWriteKind:
    if value in STATE_WRITE_KIND_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {STATE_WRITE_KIND_VALUES!r}")
