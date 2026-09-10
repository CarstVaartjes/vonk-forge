from typing import Literal, cast

FleetLogEntrySource = Literal['client', 'job', 'monitor', 'runtime']

FLEET_LOG_ENTRY_SOURCE_VALUES: set[FleetLogEntrySource] = { 'client', 'job', 'monitor', 'runtime',  }

def check_fleet_log_entry_source(value: str) -> FleetLogEntrySource:
    if value in FLEET_LOG_ENTRY_SOURCE_VALUES:
        return cast(FleetLogEntrySource, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_LOG_ENTRY_SOURCE_VALUES!r}")
