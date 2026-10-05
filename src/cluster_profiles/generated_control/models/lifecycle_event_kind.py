from typing import Literal

LifecycleEventKind = Literal['cancel-requested', 'claimed', 'heartbeat', 'lease-lapsed', 'observed', 'operator-action', 'reported', 'submitted', 'tick']

LIFECYCLE_EVENT_KIND_VALUES: set[LifecycleEventKind] = { 'cancel-requested', 'claimed', 'heartbeat', 'lease-lapsed', 'observed', 'operator-action', 'reported', 'submitted', 'tick',  }

def check_lifecycle_event_kind(value: str) -> LifecycleEventKind:
    if value in LIFECYCLE_EVENT_KIND_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIFECYCLE_EVENT_KIND_VALUES!r}")
