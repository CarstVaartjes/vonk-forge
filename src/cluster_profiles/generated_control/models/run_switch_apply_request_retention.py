from typing import Literal, cast

RunSwitchApplyRequestRetention = Literal['reclaim-unreferenced', 'retain-cached']

RUN_SWITCH_APPLY_REQUEST_RETENTION_VALUES: set[RunSwitchApplyRequestRetention] = { 'reclaim-unreferenced', 'retain-cached',  }

def check_run_switch_apply_request_retention(value: str) -> RunSwitchApplyRequestRetention:
    if value in RUN_SWITCH_APPLY_REQUEST_RETENTION_VALUES:
        return cast(RunSwitchApplyRequestRetention, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_APPLY_REQUEST_RETENTION_VALUES!r}")
