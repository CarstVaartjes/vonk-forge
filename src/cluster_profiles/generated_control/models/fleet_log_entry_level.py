from typing import Literal

FleetLogEntryLevel = Literal['debug', 'error', 'info', 'warning']

FLEET_LOG_ENTRY_LEVEL_VALUES: set[FleetLogEntryLevel] = { 'debug', 'error', 'info', 'warning',  }

def check_fleet_log_entry_level(value: str) -> FleetLogEntryLevel:
    if value in FLEET_LOG_ENTRY_LEVEL_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_LOG_ENTRY_LEVEL_VALUES!r}")
