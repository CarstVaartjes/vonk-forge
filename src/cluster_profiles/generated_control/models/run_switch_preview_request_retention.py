from typing import Literal, cast

RunSwitchPreviewRequestRetention = Literal['reclaim-unreferenced', 'retain-cached']

RUN_SWITCH_PREVIEW_REQUEST_RETENTION_VALUES: set[RunSwitchPreviewRequestRetention] = { 'reclaim-unreferenced', 'retain-cached',  }

def check_run_switch_preview_request_retention(value: str) -> RunSwitchPreviewRequestRetention:
    if value in RUN_SWITCH_PREVIEW_REQUEST_RETENTION_VALUES:
        return cast(RunSwitchPreviewRequestRetention, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_PREVIEW_REQUEST_RETENTION_VALUES!r}")
