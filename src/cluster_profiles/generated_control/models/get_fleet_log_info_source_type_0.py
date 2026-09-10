from typing import Literal, cast

GetFleetLogInfoSourceType0 = Literal['client', 'job', 'monitor', 'runtime']

GET_FLEET_LOG_INFO_SOURCE_TYPE_0_VALUES: set[GetFleetLogInfoSourceType0] = { 'client', 'job', 'monitor', 'runtime',  }

def check_get_fleet_log_info_source_type_0(value: str) -> GetFleetLogInfoSourceType0:
    if value in GET_FLEET_LOG_INFO_SOURCE_TYPE_0_VALUES:
        return cast(GetFleetLogInfoSourceType0, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {GET_FLEET_LOG_INFO_SOURCE_TYPE_0_VALUES!r}")
